#!/usr/bin/env node
/*
 * ACTUALLY RENDER the dashboard's components against a real API document.
 *
 * WHY THIS EXISTS. scripts/check_jsx.js proves the file is valid JavaScript.
 * It cannot prove the page works, and twice it did not:
 *
 *   v1.0.69  a useFetch() added below an early return -> hooks changed count
 *            between renders -> blank Maintenance tab.
 *   v1.0.71  an edit spanning `const X` .. `const path` deleted `const Y`,
 *            which sat between them -> ReferenceError inside the path map ->
 *            blank Maintenance tab.
 *
 * Both parse cleanly. Both are caught the moment you render the component
 * once. That is all this does: transpile the page's JSX, evaluate it with
 * browser stubs, and server-render
 *
 *   - the Evosep charts against a real evosep_column_health.json for every
 *     window the UI offers, and
 *   - the PEG tab (v1.2.0) end to end -- PegTab itself, not its parts --
 *     against an /api/peg/overview document plus relay responses.
 *
 *     npm install --no-save @babel/standalone react react-dom
 *     node scripts/render_check.js [path/to/evosep_column_health.json] [--peg DIR]
 *
 * --peg DIR reads PEG fixtures from DIR: peg_overview.json (required) and,
 * when present, peg_overview_empty.json, relay_leaderboard.json,
 * relay_leaderboard_empty.json, relay_trend.json, relay_lc_compare.json and
 * relay_lc_compare_both.json. Without --peg the PEG checks run on a small
 * synthetic document built below, so they always run. With --peg and no
 * Evosep document, the Evosep checks are skipped rather than failing.
 *
 * Exit 0 = every component rendered. Exit 1 = one threw, with the stack.
 * Exit 2 = missing dependency or input.
 */
const fs = require('fs');
const path = require('path');

let Babel, React, ReactDOMServer;
try {
  Babel = require('@babel/standalone');
  React = require('react');
  ReactDOMServer = require('react-dom/server');
} catch (e) {
  console.error('missing deps. Run:  npm install --no-save @babel/standalone react react-dom');
  console.error(String(e.message));
  process.exit(2);
}

const args = process.argv.slice(2);
let pegDir = null;
const positional = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--peg') { pegDir = args[++i]; if (!pegDir) { console.error('--peg needs a directory'); process.exit(2); } }
  else positional.push(args[i]);
}

const htmlPath = path.join(__dirname, '..', 'stan', 'dashboard', 'public', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');
const m = /<script[^>]*type=["']text\/babel["'][^>]*>([\s\S]*?)<\/script>/.exec(html);
if (!m) { console.error('no <script type="text/babel"> block found'); process.exit(2); }

const code = Babel.transform(m[1], { presets: [['react', { runtime: 'classic' }]] }).code;

/* Browser stubs. The page mounts itself on the last line; createRoot is
   neutered so evaluating the module does not try to paint anything. */
const noop = () => {};
const el = { addEventListener: noop, removeEventListener: noop, style: {}, classList: { add: noop, remove: noop, toggle: noop }, setAttribute: noop, appendChild: noop };
const documentStub = {
  getElementById: () => el, querySelector: () => el, querySelectorAll: () => [],
  createElement: () => el, addEventListener: noop, removeEventListener: noop,
  documentElement: el, body: el, head: el, title: '', cookie: '',
};
const storage = { getItem: () => null, setItem: noop, removeItem: noop };
const windowStub = {
  location: { origin: 'https://ucd.stan-proteomics.org', href: '/', pathname: '/', search: '', hash: '' },
  addEventListener: noop, removeEventListener: noop, localStorage: storage,
  sessionStorage: storage, matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: (f) => setTimeout(f, 0), devicePixelRatio: 1,
};
const ReactDOMStub = { createRoot: () => ({ render: noop, unmount: noop }), render: noop };

const names = [
  'EvBaselineChart', 'evBaselineSkeleton', 'EvWashFlow', 'EvColumnLifetimes', 'EvColumnAging',
  // PEG Watch (v1.2.0). PEG_CACHE is how fixtures reach PegTab: the tab
  // reads it synchronously on first render, and server rendering never
  // runs the effects that would fetch.
  'PegTab', 'PegBoardView', 'PegLcView', 'PegBadge', 'Sparkline',
  'PegBoard', 'PegImpact', 'PegTabLoad', 'PegLoadError', 'pegDefaultSpd',
  'PEG_CACHE', 'PEG_RELAY_DEFAULT', 'pegBoardUrl', 'pegLcUrl', 'pegTrendUrl',
];
const factory = new Function(
  'React', 'ReactDOM', 'window', 'document', 'localStorage', 'sessionStorage',
  'fetch', 'navigator', 'location', 'alert', 'console',
  `${code}\n;return { ${names.map(n => `${n}: typeof ${n} !== 'undefined' ? ${n} : null`).join(', ')} };`
);

let exported;
try {
  exported = factory(React, ReactDOMStub, windowStub, documentStub, storage, storage,
                     () => Promise.resolve({ ok: true, json: async () => ({}) }),
                     { userAgent: 'node' }, windowStub.location, noop, console);
} catch (e) {
  console.error('FAIL: evaluating the page module threw');
  console.error(e.stack);
  process.exit(1);
}

let fails = 0, rendered = 0;

function render(name, props) {
  try {
    return ReactDOMServer.renderToStaticMarkup(React.createElement(exported[name], props));
  } catch (e) {
    console.error(`FAIL  ${name}: ${e.message}`);
    console.error(e.stack.split('\n').slice(0, 4).join('\n'));
    fails++; return null;
  }
}

/* ======================================================================
   Evosep column charts
   ====================================================================== */
function checkEvosep(doc) {
  const flagsByRun = {};
  (doc.flags || []).forEach(f => { flagsByRun[f.start] = f; });
  const analytical = Object.values(doc.methods || {}).filter(m2 => m2.analytical);
  if (!analytical.length) { console.error('document has no analytical methods'); process.exit(2); }

  /* Every window the UI offers, including the 'This column' derivation. */
  const colDays = Math.max(14, Math.ceil((doc.column && doc.column.days_since || 0) + 3));
  /* The boundary the panel draws must be the DETECTED one where it exists, not
     the logged event: a logged date carries a placeholder time (05:00 against a
     real 11:50 change on 2026-09-02), which drew the rule 6.8 h early and put the
     old column's 520 bar cut-out on the new column's side. */
  const curLife = (doc.column_lifetimes || {}).current || {};
  const installMark = curLife.installed || (doc.column && doc.column.installed);
  if (curLife.installed && doc.column && doc.column.installed
      && curLife.installed !== doc.column.installed) {
    console.log(`note  install boundary: drawing detected ${curLife.installed}, ` +
                `not logged ${doc.column.installed}`);
  }
  const WINDOWS = [['This column', colDays], ['90 days', 90], ['1 year', 365], ['All', 0]];

  for (const [label, sinceDays] of WINDOWS) {
    for (const ms of analytical) {
      let out;
      try {
        out = ReactDOMServer.renderToStaticMarkup(
          React.createElement(exported.EvBaselineChart, {
            method: ms.method, ms, hue: '#60a5fa', flagsByRun, sinceDays,
            installedAt: installMark,
          }));
      } catch (e) {
        console.error(`FAIL  ${label.padEnd(12)} ${ms.method}: ${e.message}`);
        console.error(e.stack.split('\n').slice(0, 4).join('\n'));
        fails++; continue;
      }
      if (out === null || out === '') { console.log(`skip  ${label.padEnd(12)} ${ms.method} (too few points)`); continue; }
      rendered++;
      /* A chart that renders but plots nothing is still a broken chart. */
      if (!/<path /.test(out)) { console.error(`FAIL  ${label.padEnd(12)} ${ms.method}: no <path> in output`); fails++; continue; }
      const dates = (out.match(/>(\d{4}-\d{2}-\d{2} \d{2}:\d{2})</g) || []).map(s => s.slice(1, -1));
      const mode = /per run ·/.test(out) ? 'per-run' : (/baseline history ·/.test(out) ? 'baseline' : '???');
      const bound = /column installed/.test(out) ? ' +boundary' : '';
      /* The install rule must appear whenever the change falls inside the drawn
         window -- that marker is the only thing separating this column's data
         from the previous column's. */
      const inst = doc.column && doc.column.installed && new Date(doc.column.installed).getTime();
      const lo = new Date(dates[0]).getTime(), hi = new Date(dates[dates.length - 1]).getTime();
      if (inst && inst > lo && inst < hi && !bound) {
        console.error(`FAIL  ${label.padEnd(12)} ${ms.method}: install ${doc.column.installed} is inside the window but no boundary drawn`);
        fails++; continue;
      }
      console.log(`ok    ${label.padEnd(12)} ${ms.method.padEnd(22)} ${mode.padEnd(9)} axis ${dates[0] || '?'} -> ${dates[dates.length - 1] || '?'}${bound}`);
    }
  }
  /* THE SILENT EM-DASH. evNum(undefined) renders "—", so a field-name drift
     between the extractor and the page shows up as a blank tile under a live
     count rather than as an error. `b.median` vs `b.median_ul_min` sat that way
     until Brett spotted it on 2026-09-03. Assert the numbers the document
     actually carries are the numbers on screen. */
  const wf = doc.wash_flow, cl = doc.column_lifetimes;
  if (wf && wf.available) {
    const out = render('EvWashFlow', { wf });
    if (out !== null) {
      rendered++;
      let bad = 0;
      (wf.by_segment || []).forEach(b => {
        if (b.median_ul_min == null) return;
        /* Scope the assertion to THIS SEGMENT'S TILE. A bare out.includes() passes
           on a broken build: the same number turns up in some data point's hover
           title, so the check found 2.284 in the chart while the tile said "—". */
        const label = `Column ${String(b.installed || '?').slice(0, 10)}`;
        const at = out.indexOf(label);
        if (at < 0) {
          console.error(`FAIL  EvWashFlow: no tile for segment ${b.installed}`); bad++; return;
        }
        const tile = out.slice(at, at + 600);
        const want = b.median_ul_min.toFixed(3);
        if (tile.includes('—')) {
          console.error(`FAIL  EvWashFlow: tile "${label}" renders an em-dash; ` +
                        `document has median_ul_min ${b.median_ul_min}`);
          bad++;
        } else if (!tile.includes(want)) {
          console.error(`FAIL  EvWashFlow: tile "${label}" does not show ${want}`);
          bad++;
        }
      });
      if (bad) fails += bad;
      else console.log(`ok    EvWashFlow          ${(wf.by_segment || []).length} segment median(s) rendered`);
    }
  }
  if (cl && cl.available) {
    const out = render('EvColumnLifetimes', { cl });
    if (out !== null) { rendered++; console.log(`ok    EvColumnLifetimes   ${cl.n_columns} columns`); }
  }
  if (wf && cl) {
    const out = render('EvColumnAging', { wf, cl });
    if (out !== null) { rendered++; console.log('ok    EvColumnAging'); }
  }
}

/* ======================================================================
   PEG Watch tab
   ====================================================================== */

/* A small, deterministic stand-in for /api/peg/overview and the relay, so
   the PEG checks run on any checkout. Shapes follow spec §4.2 / §4.5; the
   numbers are made up. */
function syntheticPeg() {
  const asOf = '2026-09-28', end = Date.parse(asOf + 'T23:59:00Z'), DAY = 864e5;
  const runs = [];
  for (let k = 0; k < 240; k++) {
    const t = new Date(end - (600 - k * 2.5) * DAY);
    // Mostly 100 SPD, so the tab's default cohort is the board seeded below.
    const spd = [100, 100, 60, 100, 30][k % 5];
    const pct = k % 7 === 0 ? 0 : Math.round(((k * 37) % 100) / 12 * 1000) / 1000;
    const cls = pct < 0.5 ? 0 : pct < 2 ? 1 : pct < 5 ? 2 : 3;
    runs.push([t.toISOString().slice(0, 16), spd, pct, Math.min(100, pct * 9), cls ? 3 + cls * 2 : 0, cls, 30000 + (k % 11) * 900]);
  }
  const days = Math.floor((end - Date.parse('2025-02-05T00:00:00Z')) / DAY) + 1;
  const roll = Array.from({ length: days }, (_, i) => (i % 40 < 5 ? null : Math.round((1 + Math.sin(i / 30)) * 100) / 100));
  const weekly = Array.from({ length: 26 }, (_, i) => (i === 3 ? null : 0.5 + (i % 5) / 4));
  const ov = {
    as_of: asOf, instrument: 'timsTOF HT', instrument_family: 'timsTOF', lc_system: 'evosep',
    instruments: [{ instrument: 'timsTOF HT', n_runs: runs.length, evosep: true }],
    runs_cols: ['t', 'spd', 'pct', 'score', 'ions', 'cls', 'prec'], runs,
    rolling_start: '2025-02-05', rolling: { all: roll, 100: roll, 60: roll, 30: roll },
    episodes: [{ start: '2026-01-10', end: '2026-03-20', days: 70, n: 30, median_pct: 4.2, heavy_pct: 35, ongoing: false }],
    baseline: { median_pct: 0.2, start: '2025-03-01', end: '2025-05-29', n: 40 },
    summary: { n_30d: 12, median_30d: 1.4, median_prev_30d: 3.1, change_pct: -55, clean_30d: 4, heavy_30d: 2, clean_rate_30d: 33, streak_clean: 1 },
    ladder: { months: ['2026-07', '2026-08', '2026-09'], n: [4, 5, 6, 7, 8, 9, 10],
              share: [[0, 0.1, 0], [0.2, 0.3, 0.1], [0.5, 0.4, 0.3], [0.6, 0.7, 0.5], [0.4, 0.3, 0.2], [0.1, 0, 0.1], [0, 0, 0]],
              nruns: [20, 22, 9], adducts: { '+H': 30, '+NH4': 60, '+Na': 10 } },
    column_periods: [{ installed: '2026-02-01', retired: '2026-06-01', n_qc: 40, median_pct: 3.3, heavy_pct: 30, clean_pct: 20 },
                     { installed: '2026-06-01', retired: null, n_qc: 45, median_pct: 0.4, heavy_pct: 5, clean_pct: 70 }],
    impact: { 60: { clean: [40, 41000], trace: [20, 39000], moderate: [10, 38500], heavy: [12, 37000] } },
    lab_lc: [{ instrument: 'timsTOF HT', family: 'timsTOF', lc_system: 'evosep', n_90d: 40, median_90d: 1.2, clean_rate_90d: 40, n_365d: 160, median_365d: 1.0, weekly },
             { instrument: 'Orbitrap Exploris 480', family: 'Orbitrap', lc_system: 'custom', n_90d: 60, median_90d: 0.2, clean_rate_90d: 80, n_365d: 200, median_365d: 0.2, weekly }],
    sharing: { enabled: true, display_name: 'Synthetic Lab', relay_url: 'https://brettsp-stan.hf.space' },
  };
  const lb = {
    generated_at: asOf + 'T12:00:00Z', as_of: asOf, window_days: 30, family: 'timsTOF', spd: 100,
    cohorts: [{ family: 'timsTOF', spd: 100, n_labs: 3, n_runs_365d: 300 }, { family: 'timsTOF', spd: 60, n_labs: 1, n_runs_365d: 80 }],
    ranked: [
      { rank: 1, display_name: 'Other Lab', verified: true, instrument_models: ['timsTOF Ultra'], n_runs: 20, median_pct: 0.1, clean_pct: 90, heavy_pct: 0, change_pct: -30, weekly: weekly.slice(0, 12), badges: ['cleanest', 'most_improved'] },
      { rank: 2, display_name: 'Synthetic Lab', verified: true, instrument_models: ['timsTOF HT'], n_runs: 12, median_pct: 1.4, clean_pct: 33, heavy_pct: 17, change_pct: null, weekly: weekly.slice(0, 12), badges: [] },
      { rank: 3, display_name: '<img src=x onerror=alert(1)>', verified: false, instrument_models: ['timsTOF Pro 2'], n_runs: 6, median_pct: 5.5, clean_pct: 10, heavy_pct: 50, change_pct: 20, weekly: weekly.slice(0, 12), badges: [] },
    ],
    unranked: [{ display_name: 'Tiny Lab', verified: false, n_runs: 2 }],
    community: { n_labs: 3, n_runs: 38, p25_pct: 0.1, median_pct: 1.4, p75_pct: 5.5 },
  };
  const lbEmpty = Object.assign({}, lb, { spd: 60, ranked: [], unranked: [], community: { n_labs: 0, n_runs: 0, p25_pct: null, median_pct: null, p75_pct: null } });
  const trend = { weeks: Array.from({ length: 52 }, (_, i) => ({ week_start: new Date(end - (52 - i) * 7 * DAY).toISOString().slice(0, 10), n_labs: 3, n_runs: 20, p25: 0.1, p50: 0.5, p75: 2 })) };
  const lcOne = { family: 'timsTOF', window_days: 90, as_of: asOf,
    groups: [{ lc: 'evosep', n_labs: 3, n_runs: 120, p25_pct: 0.1, median_pct: 1.1, p75_pct: 4, clean_pct: 45, heavy_pct: 20, weekly },
             { lc: 'other', n_labs: 0, n_runs: 0, p25_pct: null, median_pct: null, p75_pct: null, clean_pct: null, heavy_pct: null, weekly: [] }],
    families: [{ family: 'timsTOF', evosep_runs: 120, other_runs: 0, evosep_labs: 3, other_labs: 0 }] };
  const lcBoth = JSON.parse(JSON.stringify(lcOne));
  lcBoth.groups[1] = { lc: 'other', n_labs: 2, n_runs: 70, p25_pct: 0.02, median_pct: 0.15, p75_pct: 0.7, clean_pct: 80, heavy_pct: 2, weekly };
  const ovEmpty = Object.assign({}, ov, { runs: [], rolling: {}, episodes: [], baseline: null, lab_lc: [], impact: {}, column_periods: [],
    ladder: { months: [], n: [], share: [], nruns: [], adducts: {} },
    summary: { n_30d: 0, median_30d: null, median_prev_30d: null, change_pct: null, clean_30d: 0, heavy_30d: 0, clean_rate_30d: null, streak_clean: 0 } });
  return { source: 'built-in synthetic', ov, ovEmpty, lb, lbEmpty, trend, lcOne, lcBoth };
}

function loadPegFixtures(dir) {
  const read = (f, required) => {
    const p = path.join(dir, f);
    if (!fs.existsSync(p)) {
      if (required) { console.error(`no PEG fixture at ${p}`); process.exit(2); }
      return null;
    }
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  };
  const syn = syntheticPeg();
  return {
    source: dir,
    ov: read('peg_overview.json', true),
    ovEmpty: read('peg_overview_empty.json') || syn.ovEmpty,
    lb: read('relay_leaderboard.json') || syn.lb,
    lbEmpty: read('relay_leaderboard_empty.json') || syn.lbEmpty,
    trend: read('relay_trend.json') || syn.trend,
    lcOne: read('relay_lc_compare.json') || syn.lcOne,
    lcBoth: read('relay_lc_compare_both.json') || syn.lcBoth,
  };
}

/* React reports duplicate keys and bad props through console.error; on a
   list of 1,700 circles a duplicate key is a real bug (React may drop or
   reuse the wrong node), so count those as failures. */
function renderQuiet(name, props) {
  const warnings = [];
  const orig = console.error;
  console.error = (...a) => { warnings.push(a.map(String).join(' ')); };
  let out = null, err = null;
  try {
    out = ReactDOMServer.renderToStaticMarkup(React.createElement(exported[name], props));
  } catch (e) {
    err = e;
  } finally {
    console.error = orig;
  }
  if (err) {
    console.error(`FAIL  ${name}: ${err.message}`);
    console.error(String(err.stack).split('\n').slice(0, 4).join('\n'));
    fails++;
  }
  warnings.forEach(w => {
    console.error(`FAIL  ${name}: React warned: ${w.split('\n')[0].slice(0, 240)}`);
    fails++;
  });
  return out;
}

function checkPeg(fx) {
  const E = exported;
  const missing = ['PegTab', 'PegBoardView', 'PegLcView', 'PegBoard', 'PegImpact', 'PegTabLoad', 'PegLoadError',
                   'PEG_CACHE', 'pegBoardUrl', 'pegLcUrl', 'pegTrendUrl'].filter(n => !E[n]);
  if (missing.length) { console.error(`FAIL  PEG: page does not define ${missing.join(', ')}`); fails++; return; }
  console.log(`\nPEG Watch (fixtures: ${fx.source})`);
  const ov = fx.ov;
  const relay = ((ov.sharing && ov.sharing.relay_url) || E.PEG_RELAY_DEFAULT).replace(/\/+$/, '');
  const fam = ov.instrument_family;
  const now = Date.now();
  const seed = (url, data) => E.PEG_CACHE.set(url, { t: now, data });
  const seedAll = (overview) => {
    E.PEG_CACHE.clear();
    seed('/api/peg/overview', overview);
    /* Seed every cohort x window the UI can ask for: the fixture board is
       spec's default cohort (100 SPD), every other one is empty. */
    for (const spd of ['100', '60', '30']) {
      for (const win of ['30', '90', '365']) seed(E.pegBoardUrl(relay, fam, spd, win), spd === String(fx.lb.spd) ? fx.lb : fx.lbEmpty);
      seed(E.pegTrendUrl(relay, fam, spd), fx.trend);
    }
    seed(E.pegLcUrl(relay, fam), fx.lcOne);
  };
  const expect = (label, cond, detail) => {
    if (cond) return true;
    console.error(`FAIL  PegTab: ${label}${detail ? ` (${detail})` : ''}`);
    fails++; return false;
  };
  const text = (h) => h.replace(/<[^>]+>/g, ' ');

  /* 1. The whole tab, every panel fed. */
  seedAll(ov);
  const t0 = process.hrtime.bigint();
  const out = renderQuiet('PegTab', {});
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  if (out !== null) {
    rendered++;
    const order = ['PEG over time', 'Every QC day since', 'Community PEG leaderboard', 'Evosep vs other LC',
                   'What PEG costs you', 'PEG ladder fingerprint', 'column period', 'Isolate the source',
                   'What your lab shares', 'How the ranking works'];
    let at = -1, okOrder = true;
    for (const h of order) {
      const i = out.indexOf(h);
      if (!expect(`section "${h}" rendered in mockup order`, i > at, i < 0 ? 'missing' : `at ${i}, previous at ${at}`)) okOrder = false;
      if (i > at) at = i;
    }
    const tx = text(out);
    expect('no NaN / undefined / [object Object] on screen', !/\bNaN\b|\bundefined\b|\[object Object\]/.test(tx),
           (tx.match(/.{0,40}(\bNaN\b|\bundefined\b|\[object Object\]).{0,40}/) || [''])[0]);
    expect('no mockup placeholder rows', !/\bExample\b/.test(tx));
    expect('best-90-day baseline labelled', !ov.baseline || /your best 90 days \(/.test(tx));
    // Every run in the default range ("Since <previous year>") is a circle.
    const asOfEnd = Date.parse(String(ov.as_of).slice(0, 10) + 'T23:59:00Z');
    const from = Date.UTC(new Date(asOfEnd).getUTCFullYear() - 1, 0, 1);
    const cols = ov.runs_cols || ['t'];
    const ti = Math.max(0, cols.indexOf('t'));
    const want = ov.runs.filter(r => { const t = Date.parse(String(r[ti]) + ':00Z'); return t >= from && t <= asOfEnd; }).length;
    const got = (out.match(/class="peg-pt /g) || []).length;
    expect('timeline plots every run in the default range', got === want, `${got} circles, ${want} runs`);
    const rows = (out.match(/<tr class="peg-(you|oth)"/g) || []).length;
    expect('leaderboard shows every ranked lab the relay returned', rows === (fx.lb.ranked || []).length, `${rows} rows`);
    const me = ov.sharing && ov.sharing.display_name;
    const meRanked = (fx.lb.ranked || []).some(r => r.display_name === me);
    if (meRanked) expect('your row highlighted with a YOU tag', /<tr class="peg-you"[\s\S]*?YOU/.test(out));
    /* Off Evosep the board shown is the family's Evosep labs, for
       comparison; this lab has no rank there whatever the relay says. */
    if (ov.lc_system !== 'evosep') expect('rank tile says the board ranks Evosep runs only', /Community rank[\s\S]{0,300}ranks Evosep runs only/.test(out));
    else if (meRanked) expect('rank tile shows your rank', /Community rank[\s\S]{0,200}#\d/.test(out));
    const hostile = [...(fx.lb.ranked || []), ...(fx.lb.unranked || [])].find(r => /[<>&]/.test(r.display_name));
    if (hostile) {
      expect('relay display_name is escaped, never markup', !out.includes(hostile.display_name)
             && out.includes(hostile.display_name.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')));
    }
    const lab = ov.lab_lc || [];
    const ev = new Set(lab.filter(x => x.lc_system === 'evosep').map(x => x.family));
    // A null lc_system is "no LC recorded", not another LC (review UI-7).
    const ot = new Set(lab.filter(x => x.lc_system && x.lc_system !== 'evosep').map(x => x.family));
    const cross = ev.size && ot.size && ![...ev].some(f => ot.has(f));
    expect('cross-family caveat matches lab_lc', /Different instrument families/.test(out) === !!cross);
    expect('one lab_lc row per instrument', (out.match(/class="peg-lc-row"/g) || []).length === lab.length);
    const lcBoth = (fx.lcOne.groups || []).every(g => g.n_runs > 0);
    expect('LC comparison empty state until a family has both groups', /Nothing to compare on/.test(out) === !lcBoth);
    /* The calendar and ladder pin their scroller to the newest column, so a
       row label drawn inside the scrolled SVG is scrolled out of view with
       the oldest weeks (M/W/F rendered as '/I', '/V' at 1280 px; the ladder
       lost every n label at phone width). They belong before the scroller. */
    const sectionOf = (head) => { const a = out.indexOf(head); return a < 0 ? '' : out.slice(a, out.indexOf('</section>', a)); };
    const beforeScroller = (sec, label) => {
      const lab = sec.indexOf(label), sc = sec.indexOf('class="peg-scrollx"');
      return lab >= 0 && sc >= 0 && lab < sc;
    };
    expect('calendar day labels sit outside its scroller', beforeScroller(sectionOf('Every QC day since'), '>M</text>'));
    if ((ov.ladder && ov.ladder.months || []).length) {
      expect('ladder n labels sit outside its scroller', beforeScroller(sectionOf('PEG ladder fingerprint'), '>PEG n</text>'));
    }
    /* A best-90-day median of 0% sits on the chart floor under the jittered
       clean dots; its label must be painted after them, over a halo. */
    if (ov.baseline && ov.baseline.median_pct != null) {
      const bm = /<text[^>]*class="peg-base-t"[^>]*>your best 90 days \(/.exec(out);
      expect('best-90-day label painted over the dots, with a halo',
             bm && bm.index > out.lastIndexOf('class="peg-pt '), bm ? 'drawn under the dots' : 'no peg-base-t label');
    }
    console.log(`ok    PegTab              ${got} runs plotted · ${rows} board rows · ${lab.length} lab_lc rows` +
                `${cross ? ' · cross-family note' : ''} · ${ms.toFixed(0)} ms${okOrder ? '' : ' · ORDER WRONG'}`);
  }

  /* 2. Relay not answered yet: both community panels say so, the rest renders. */
  E.PEG_CACHE.clear();
  seed('/api/peg/overview', ov);
  const loading = renderQuiet('PegTab', {});
  if (loading !== null) {
    rendered++;
    if (expect('loading states while the relay has not answered',
               /Loading the community board/.test(loading) && /Loading the community comparison/.test(loading)
               && /PEG over time/.test(loading))) console.log('ok    PegTab              relay loading states');
  }

  /* 3. Instrument with no real PEG runs. */
  E.PEG_CACHE.clear();
  seed('/api/peg/overview', fx.ovEmpty);
  const empty = renderQuiet('PegTab', {});
  if (empty !== null) {
    rendered++;
    if (expect('empty state explains the readers', /No PEG measurements yet/.test(empty) && /install-peg-deps/.test(empty)))
      console.log('ok    PegTab              empty state (no real PEG runs)');
  }

  /* 3b. Side panels the server could not read are named in `degraded`; the
     tab must say "could not be read", not "nothing recorded". */
  E.PEG_CACHE.clear();
  seed('/api/peg/overview', Object.assign({}, ov, {
    degraded: ['ladder', 'column_periods', 'lab_lc'], column_periods: [], lab_lc: [],
    ladder: { months: [], n: [], share: [], nruns: [], adducts: {} } }));
  const deg = renderQuiet('PegTab', {});
  if (deg !== null) {
    rendered++;
    const k = (deg.match(/could not be read from the database/g) || []).length;
    if (expect('degraded side panels say so', k === 4, `${k} of 4 notes`)) console.log('ok    PegTab              degraded side panels');
  }

  /* 4. Nothing cached at all: the tab's own loading state. */
  E.PEG_CACHE.clear();
  const cold = renderQuiet('PegTab', {});
  if (cold !== null) { rendered++; if (expect('loading state', /Loading PEG history/.test(cold))) console.log('ok    PegTab              loading state'); }

  /* 5. Board states the full render cannot reach without a network. */
  const boardProps = { ov, family: fam, isEvosep: true, chips: ['100', '60', '30'], coh: '100', win: '30',
                       onCoh: () => {}, onWin: () => {}, myName: ov.sharing && ov.sharing.display_name, relayBase: relay };
  const down = renderQuiet('PegBoardView', Object.assign({}, boardProps, { data: null, error: 'Failed to fetch' }));
  if (down !== null) { rendered++; if (expect('relay-unreachable note', /Community board unavailable/.test(down) && /brettsp-stan\.hf\.space|\w+\.\w+/.test(down))) console.log('ok    PegBoardView        relay unreachable'); }
  const none = renderQuiet('PegBoardView', Object.assign({}, boardProps, { data: fx.lbEmpty, coh: '60' }));
  if (none !== null) { rendered++; if (expect('empty board state', /No other labs yet/.test(none) && !/<table/.test(none))) console.log('ok    PegBoardView        no labs yet'); }
  const orbi = renderQuiet('PegBoardView', Object.assign({}, boardProps, { isEvosep: false, family: 'Orbitrap', data: fx.lbEmpty,
                          ov: Object.assign({}, ov, { instrument: 'Orbitrap Exploris 480', lc_system: 'custom' }) }));
  if (orbi !== null) { rendered++; if (expect('non-Evosep note', /not Evosep, so your lab is not on this board/.test(orbi))) console.log('ok    PegBoardView        non-Evosep instrument'); }

  /* 6. LC comparison with both groups present. */
  const lcProps = { labLc: ov.lab_lc || [], fam, onFam: () => {}, relayBase: relay };
  const both = renderQuiet('PegLcView', Object.assign({}, lcProps, { data: fx.lcBoth }));
  if (both !== null) {
    rendered++;
    const g = (both.match(/class="peg-lc-group"/g) || []).length;
    if (expect('both LC groups side by side', g === 2, `${g} groups`)) console.log('ok    PegLcView           Evosep + other LC groups');
  }
  const lcDown = renderQuiet('PegLcView', Object.assign({}, lcProps, { data: null, error: 'HTTP 502' }));
  if (lcDown !== null) { rendered++; if (expect('LC relay-unreachable note', /Community comparison unavailable/.test(lcDown))) console.log('ok    PegLcView           relay unreachable'); }

  /* 7. The bug fixes shipped with the tab (spec §4.6). These components
     predate PEG Watch, so they go through plain render(): React 19's server
     renderer warns about Sparkline's multi-part <title> text, which the
     browser's React 18 renders fine, and that is not this check's business. */
  const unk = render('PegBadge', { run: { id: 1, peg_class: 'unknown', peg_score: 0, peg_intensity_pct: 0, peg_n_ions_detected: 0 } });
  const cln = render('PegBadge', { run: { id: 2, peg_class: 'clean', peg_score: 0, peg_intensity_pct: 0, peg_n_ions_detected: 0 } });
  if (unk !== null && cln !== null) {
    rendered += 2;
    if (expect("PegBadge 'unknown' renders n/a, not a clean 0.0", />n\/a</.test(unk) && !/>0\.0</.test(unk))
        && expect('PegBadge real clean zero still renders 0.0', />0\.0</.test(cln))) console.log("ok    PegBadge            'unknown' -> n/a; clean 0 -> 0.0");
  }
  const mk = (score, cls, i) => ({ id: i, run_date: `2026-09-${String(10 + i).padStart(2, '0')}T12:00:00Z`, spd: 100,
                                   peg_score: score, peg_class: cls, peg_intensity_pct: score / 10, peg_n_ions_detected: 3 });
  // Zeros are real clean runs: median of [0,0,0,30,80] is 0 (it was 80 when zeros were dropped).
  const quiet = (name, props) => {
    const orig = console.error;
    // Mute React's warnings, but never render()'s own FAIL line and stack.
    console.error = (...a) => { const s = String(a[0]); if (s.startsWith('FAIL') || /\n\s+at /.test(s)) orig(...a); };
    try { return render(name, props); } finally { console.error = orig; }
  };
  const zeros = quiet('Sparkline', { runs: [mk(0, 'clean', 1), mk(0, 'clean', 2), mk(0, 'clean', 3), mk(30, 'trace', 4), mk(80, 'heavy', 5)], metric: 'peg_score', label: 'PEG' });
  // The 'unknown' sentinel is not a zero: median of [0,30,80] is 30 (0 if the sentinels counted).
  const sentinel = quiet('Sparkline', { runs: [mk(0, 'clean', 1), mk(30, 'trace', 2), mk(80, 'heavy', 3), mk(0, 'unknown', 4), mk(0, 'unknown', 5)], metric: 'peg_score', label: 'PEG' });
  if (zeros !== null && sentinel !== null) {
    rendered += 2;
    // Every point's hover text carries "this instrument median: N".
    const med = (h) => ((h.match(/this instrument median: ([\d.,]+)/i) || [])[1]);
    if (expect('Sparkline keeps PEG zeros in the median', med(zeros) === '0', `median ${med(zeros)}`)
        && expect("Sparkline drops the 'unknown' sentinel", med(sentinel) === '30', `median ${med(sentinel)}`))
      console.log('ok    Sparkline           peg_score zeros kept, unknown dropped');
  }

  /* 8. Review findings, 2026-09-28 (UI-1, UI-3, UI-7, UI-8). */
  const peerCoh = [...(fx.lb.cohorts || []), { family: 'Exploris', spd: 60, n_labs: 2, n_runs_365d: 90 }];
  const expl = Object.assign({}, ov, { instrument: 'Orbitrap Exploris 480', instrument_family: 'Exploris', lc_system: 'custom' });
  const explBoard = { ov: expl, isEvosep: false, family: 'Exploris', relayBase: relay, defaultSpd: '38',
                      myName: ov.sharing && ov.sharing.display_name, rankLb: { data: null, error: null } };
  const pressed = (h) => ((/<button[^>]*aria-pressed="true"[^>]*>Exploris · (\d+) SPD</.exec(h) || [])[1]);
  /* Every URL the board asks the cache for is a URL it would fetch. */
  const asked = [];
  const cacheGet = E.PEG_CACHE.get;
  E.PEG_CACHE.get = function (k) { asked.push(k); return cacheGet.call(this, k); };
  try {
    /* UI-1. A non-Evosep instrument's own SPD (38 on a 30 min Exploris
       gradient) is not a board cohort: the relay ranks Evosep methods only,
       so that board is empty for good. Nothing known yet -> 100 SPD. */
    E.PEG_CACHE.clear();
    const cold = renderQuiet('PegBoard', explBoard);
    if (cold !== null) {
      rendered++;
      const tx = text(cold);
      if (expect('non-Evosep board opens on an Evosep cohort, not its own SPD', pressed(cold) === '100', `pressed ${pressed(cold)}`)
          & expect('non-Evosep board never asks for its own SPD', !asked.some(u => /[?&]spd=38\b/.test(u)),
                   asked.filter(u => /spd=38/.test(u)).join(' '))
          & expect('non-Evosep board never names its own SPD', !/\b38 SPD\b/.test(tx)))
        console.log('ok    PegBoard            non-Evosep: Evosep cohort, not 38 SPD');
    }
    /* ...and once the relay reports Exploris labs on Evosep at 60 SPD only,
       that is the board it shows. */
    E.PEG_CACHE.clear(); asked.length = 0;
    seed(E.pegBoardUrl(relay, 'Exploris', '100', '30'), Object.assign({}, fx.lbEmpty, { family: 'Exploris', spd: 100, cohorts: peerCoh }));
    seed(E.pegBoardUrl(relay, 'Exploris', '60', '30'), Object.assign({}, fx.lb, { family: 'Exploris', spd: 60, cohorts: peerCoh }));
    const peers = renderQuiet('PegBoard', explBoard);
    if (peers !== null) {
      rendered++;
      const tx = text(peers);
      if (expect('non-Evosep board follows the relay to the cohort with labs', pressed(peers) === '60', `pressed ${pressed(peers)}`)
          & expect('non-Evosep board shows those labs', (peers.match(/<tr class="peg-(you|oth)"/g) || []).length === (fx.lb.ranked || []).length)
          & expect('no 38 SPD chip or request', !/\b38 SPD\b/.test(tx) && !asked.some(u => /[?&]spd=38\b/.test(u)))
          & expect('says why this lab is not on the board', /custom LC, not Evosep/.test(tx) && /Exploris labs on Evosep/.test(tx)))
        console.log('ok    PegBoard            non-Evosep: follows relay cohorts to Exploris · 60 SPD');
    }
    /* No Exploris lab on Evosep at all: say that, and do not tell a lab
       that cannot join how to join. */
    E.PEG_CACHE.clear();
    seed(E.pegBoardUrl(relay, 'Exploris', '100', '30'), Object.assign({}, fx.lbEmpty, { family: 'Exploris', spd: 100 }));
    const lone = renderQuiet('PegBoard', explBoard);
    if (lone !== null) {
      rendered++;
      const tx = text(lone);
      if (expect('empty non-Evosep board says no Evosep lab of its family shared', /No Exploris lab on Evosep has shared/.test(tx))
          & expect('empty non-Evosep board has no "Labs join by"', !/Labs join by/.test(tx)))
        console.log('ok    PegBoard            non-Evosep: empty, no join instructions');
    }
  } finally {
    E.PEG_CACHE.get = cacheGet;
  }

  /* Same defect on an Evosep instrument: a derived SPD (36, from a 32 min
     gradient) is no board cohort either, however often it ran. */
  if (E.pegDefaultSpd) {
    const t = Date.parse(String(ov.as_of).slice(0, 10) + 'T12:00:00Z');
    const mkRun = (spd, k) => ({ t: t - k * 864e5, spd });
    const d36 = E.pegDefaultSpd([...Array(9)].map((_, k) => mkRun(36, k)).concat([mkRun(60, 1), mkRun(60, 2)]), t + 432e5);
    const d0 = E.pegDefaultSpd([...Array(9)].map((_, k) => mkRun(36, k)), t + 432e5);
    if (expect('default cohort skips an SPD the board never ranks', d36 === '60', `got ${d36}`)
        & expect('default cohort with no board SPD at all is 100', d0 === '100', `got ${d0}`))
      console.log('ok    pegDefaultSpd       derived SPD (36) never picked as a board cohort');
  }

  /* UI-7. lc_system null means no LC was recorded. That is neither Evosep
     nor "another LC", and must not trigger the cross-family caveat. */
  const evRow = (ov.lab_lc || []).find(x => x.lc_system === 'evosep')
    || { instrument: 'timsTOF HT', family: 'timsTOF', lc_system: 'evosep', n_90d: 40, median_90d: 1.2, clean_rate_90d: 40, n_365d: 160, median_365d: 1.0, weekly: [] };
  const nullRow = { instrument: 'Orbitrap Exploris 480', family: 'Exploris', lc_system: null, n_90d: 20, median_90d: 0.3,
                    clean_rate_90d: 90, n_365d: 60, median_365d: 0.3, weekly: [] };
  const nullLc = renderQuiet('PegLcView', Object.assign({}, lcProps, { labLc: [evRow, nullRow], data: fx.lcOne }));
  if (nullLc !== null) {
    rendered++;
    if (expect('null LC is not an "Other LC"', !/>Other LC</.test(nullLc) && /peg-lcchip nr/.test(nullLc))
        & expect('null LC does not trigger the cross-family caveat', !/Different instrument families/.test(nullLc)))
      console.log('ok    PegLcView           lc_system null -> Unknown, no cross-family note');
  }
  const noLc = renderQuiet('PegBoardView', Object.assign({}, boardProps, { isEvosep: false, family: 'Exploris', data: fx.lbEmpty,
                           ov: Object.assign({}, ov, { instrument: 'Orbitrap Exploris 480', lc_system: null }) }));
  if (noLc !== null) {
    rendered++;
    if (expect('board note for an unrecorded LC', !/non-Evosep LC/.test(noLc) && /No LC is recorded/.test(noLc)))
      console.log('ok    PegBoardView        lc_system null -> "No LC is recorded"');
  }

  /* UI-3. Real UC Davis numbers: at 30 SPD clean and heavy are ~600
     precursors apart, and both value labels centred over their dots
     rendered as "43,74,332". The +1.4% there is a gain, not a loss. */
  const impact = { 100: { clean: [441, 36133], trace: [166, 33838], moderate: [57, 32097], heavy: [178, 31584] },
                   60: { clean: [407, 41424], trace: [149, 39187], moderate: [50, 39474], heavy: [146, 37061] },
                   30: { clean: [43, 43737], trace: [9, 46077], moderate: [1, 43928], heavy: [10, 44332] } };
  const imp = renderQuiet('PegImpact', { impact, firstT: Date.UTC(2024, 4, 1) });
  if (imp !== null) {
    rendered++;
    const texts = [...imp.matchAll(/<text([^>]*)>([^<]*)<\/text>/g)].map(([, a, t]) => {
      const at = (k) => ((new RegExp(`\\b${k}="([^"]*)"`).exec(a) || [])[1]);
      return { t, x: +at('x'), y: +at('y'), anchor: at('text-anchor') || 'start', cls: at('class') };
    });
    /* 11 px labels; 6.2 units a character over-estimates digits and commas. */
    const span = (e) => { const w = e.t.length * 6.2; return e.anchor === 'middle' ? [e.x - w / 2, e.x + w / 2] : e.anchor === 'end' ? [e.x - w, e.x] : [e.x, e.x + w]; };
    let clash = 0;
    for (const [k, v] of Object.entries(impact)) {
      const a = texts.find(e => e.t === v.clean[1].toLocaleString('en-US'));
      const b = texts.find(e => e.t === v.heavy[1].toLocaleString('en-US'));
      if (!a || !b) { clash++; console.error(`FAIL  PegImpact: ${k} SPD value labels missing`); fails++; continue; }
      const [a0, a1] = span(a), [b0, b1] = span(b);
      if (a.y === b.y && a0 < b1 && b0 < a1) { clash++; expect(`${k} SPD clean and heavy labels do not overlap`, false, `${a.t} [${a0.toFixed(0)}, ${a1.toFixed(0)}] vs ${b.t} [${b0.toFixed(0)}, ${b1.toFixed(0)}]`); }
    }
    const gain = texts.find(e => e.t === '+1.4%');
    if (!clash
        & expect('a gain is not painted as a loss', gain && gain.cls !== 'peg-ep-t', gain ? `class ${gain.cls}` : 'no +1.4% label')
        & expect('a loss is still red', texts.some(e => /^-\d/.test(e.t) && e.cls === 'peg-ep-t'))
        & expect('a class with n < 3 is not drawn as a median', !/n=1</.test(imp)))
      console.log('ok    PegImpact           30 SPD labels apart · +1.4% not red · n=1 dot dropped');
  }

  /* UI-8. Switching instrument must keep the tab (and its picker) on
     screen while the new overview loads, and a failed load must offer a
     way back. Server rendering never runs effects, so the switch is posed
     as "a previous overview exists, the new URL is not cached yet". */
  E.PEG_CACHE.clear();
  const multi = Object.assign({}, ov, { instruments: (ov.instruments || []).length >= 2 ? ov.instruments
    : [...(ov.instruments || []), { instrument: 'Orbitrap Exploris 480', n_runs: 5, evosep: false }] });
  const other = multi.instruments.find(i => i.instrument !== ov.instrument).instrument;
  const switching = renderQuiet('PegTabLoad', { instrument: other, onInstrument: () => {}, onRetry: () => {},
                                                lastGood: { current: { ov: multi, instrument: '' } } });
  if (switching !== null) {
    rendered++;
    if (expect('instrument switch keeps the tab mounted, dimmed', /peg-busy/.test(switching) && /PEG over time/.test(switching) && !/Loading PEG history/.test(switching))
        & expect('instrument picker stays, showing the new pick', new RegExp(`<option[^>]*selected=""[^>]*>${other.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`).test(switching)))
      console.log('ok    PegTabLoad          switch in flight: previous view kept, picker shows the pick');
  }
  const failed = renderQuiet('PegLoadError', { error: 'HTTP 503', onRetry: () => {}, back: ov.instrument, onBack: () => {} });
  if (failed !== null) {
    rendered++;
    if (expect('failed load offers Retry and a way back', />Retry</.test(failed) && new RegExp(`>Back to (<!-- -->)?${ov.instrument}<`).test(failed)))
      console.log('ok    PegLoadError        Retry + back to the previous instrument');
  }
  E.PEG_CACHE.clear();
}

/* ======================================================================
   CSS the server renderer cannot see (review, 2026-09-28)
   ====================================================================== */
function checkCss() {
  const style = (/<style>([\s\S]*?)<\/style>/.exec(html) || [])[1] || '';
  const rule = (sel) => {
    const esc = sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const m = new RegExp(`(?:^|[}\\s])${esc}\\s*\\{([^}]*)\\}`, 'm').exec(style);
    return m ? m[1] : null;
  };
  const want = (label, sel, re) => {
    const body = rule(sel);
    if (body !== null && re.test(body)) return true;
    console.error(`FAIL  CSS: ${label} (${sel} { ${body === null ? 'no such rule' : body.trim()} })`);
    fails++; return false;
  };
  /* Twelve tabs came to ~1 px over the 1168 px container and flex-shrink
     wrapped four labels onto two lines on every tab; at phone width the
     row forced the whole page to scroll sideways. */
  const a = want('tab labels never wrap', '.tab', /white-space:\s*nowrap/) & want('tabs keep their width', '.tab', /flex:\s*none/)
          & want('the tab row scrolls instead of the page', '.tabs', /overflow-x:\s*auto/);
  /* .peg-seg clips its children, so a ring drawn outside the button showed
     as slivers between buttons. It has to sit inside. */
  const b = want('segmented-toggle focus ring drawn inside the button', '.peg-root .peg-seg button:focus-visible', /outline-offset:\s*-\d/);
  if (a && b) console.log('ok    CSS                 tab row nowrap + scroll · seg focus ring inset');
}

/* ====================================================================== */
const docPath = positional[0] || '/tmp/ev_full.json';
const haveDoc = fs.existsSync(docPath);
if (haveDoc) {
  checkEvosep(JSON.parse(fs.readFileSync(docPath, 'utf8')));
} else if (pegDir) {
  console.log(`skip  Evosep charts: no API document at ${docPath}`);
} else {
  console.error(`no API document at ${docPath} — pass one as argv[1] ` +
                '(or run only the PEG checks with --peg DIR). Evosep charts NOT checked.');
}
checkCss();
checkPeg(pegDir ? loadPegFixtures(pegDir) : syntheticPeg());

console.log(`\n${rendered} render(s), ${fails} failure(s)`);
/* A missing Evosep document is still exit 2 unless the caller asked for a
   PEG-only run: "nothing to check" must not read as "checked and fine". */
process.exit(fails ? 1 : (haveDoc || pegDir ? 0 : 2));
