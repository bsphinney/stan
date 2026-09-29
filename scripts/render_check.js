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
 * relay_leaderboard_empty.json, relay_trend.json, relay_lc_compare.json (a
 * timsTOF answer with Evosep labs only), relay_lc_compare_other.json (an
 * Orbitrap family's answer with other-LC labs only),
 * relay_lc_compare_none.json (a family nobody shares) and
 * relay_lc_compare_both.json. Any other peg_overview_<name>.json is one more
 * instrument's overview (e.g. ?instrument=Orbitrap%20Exploris%20480): the
 * whole tab is rendered once per overview for the cross-family checks
 * (v1.2.1), so put a timsTOF and a non-timsTOF one there. Without --peg the
 * PEG checks run on a small synthetic document built below, so they always
 * run. With --peg and no Evosep document, the Evosep checks are skipped
 * rather than failing.
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
  'PegShare', 'pegCanonName',
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
  /* The relay always answers both groups; an empty one carries 26 null
     weeks, as the live relay's does (2026-09-29). */
  const noLab = (lc) => ({ lc, n_labs: 0, n_runs: 0, p25_pct: null, median_pct: null, p75_pct: null, clean_pct: null, heavy_pct: null,
                           weekly: Array(26).fill(null) });
  const lcFams = [{ family: 'timsTOF', evosep_runs: 120, other_runs: 0, evosep_labs: 3, other_labs: 0 },
                  { family: 'Exploris', evosep_runs: 0, other_runs: 95, evosep_labs: 0, other_labs: 1 }];
  const lcOne = { family: 'timsTOF', window_days: 90, as_of: asOf,
    groups: [{ lc: 'evosep', n_labs: 3, n_runs: 120, p25_pct: 0.1, median_pct: 1.1, p75_pct: 4, clean_pct: 45, heavy_pct: 20, weekly },
             noLab('other')],
    families: lcFams };
  const lcBoth = JSON.parse(JSON.stringify(lcOne));
  lcBoth.groups[1] = { lc: 'other', n_labs: 2, n_runs: 70, p25_pct: 0.02, median_pct: 0.15, p75_pct: 0.7, clean_pct: 80, heavy_pct: 2, weekly };
  /* The mirror image, as the relay answers an Orbitrap family today: other
     LC only. And a family no lab shares at all. */
  const lcOther = { family: 'Exploris', window_days: 90, as_of: asOf,
    groups: [noLab('evosep'),
             { lc: 'other', n_labs: 1, n_runs: 95, p25_pct: 0.135, median_pct: 0.192, p75_pct: 0.283, clean_pct: 1, heavy_pct: 0, weekly }],
    families: lcFams };
  const lcNone = { family: 'Astral', window_days: 90, as_of: asOf, groups: [noLab('evosep'), noLab('other')], families: lcFams };
  const ovEmpty = Object.assign({}, ov, { runs: [], rolling: {}, episodes: [], baseline: null, lab_lc: [], impact: {}, column_periods: [],
    ladder: { months: [], n: [], share: [], nruns: [], adducts: {} },
    summary: { n_30d: 0, median_30d: null, median_prev_30d: null, change_pct: null, clean_30d: 0, heavy_30d: 0, clean_rate_30d: null, streak_clean: 0 } });
  /* The same lab seen from its Orbitrap, for the cross-family checks. */
  const ovOrbi = Object.assign({}, ov, { instrument: 'Orbitrap Exploris 480', instrument_family: 'Exploris', lc_system: 'custom' });
  const views = [{ name: 'synthetic timsTOF', ov }, { name: 'synthetic Exploris', ov: ovOrbi }];
  return { source: 'built-in synthetic', ov, ovEmpty, lb, lbEmpty, trend, lcOne, lcBoth, lcOther, lcNone, views };
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
  const ov = read('peg_overview.json', true);
  const more = fs.readdirSync(dir).filter(f => /^peg_overview_.+\.json$/.test(f) && f !== 'peg_overview_empty.json').sort();
  return {
    source: dir,
    ov,
    views: [{ name: 'peg_overview.json', ov }, ...more.map(f => ({ name: f, ov: read(f, true) }))],
    ovEmpty: read('peg_overview_empty.json') || syn.ovEmpty,
    lb: read('relay_leaderboard.json') || syn.lb,
    lbEmpty: read('relay_leaderboard_empty.json') || syn.lbEmpty,
    trend: read('relay_trend.json') || syn.trend,
    lcOne: read('relay_lc_compare.json') || syn.lcOne,
    lcBoth: read('relay_lc_compare_both.json') || syn.lcBoth,
    lcOther: read('relay_lc_compare_other.json') || syn.lcOther,
    lcNone: read('relay_lc_compare_none.json') || syn.lcNone,
  };
}

/* The community half of the "Evosep vs other LC" panel, read off rendered
   markup: every LC slot in page order, whether it is a data card or an
   empty placeholder, and whether the whole half is the no-data note.
   Each slot's html runs to the next slot, or to the end of the section. */
function lcCommunity(h) {
  const a = h.indexOf('Community, same instrument family');
  const sec = a < 0 ? '' : h.slice(a, h.indexOf('</section>', a) < 0 ? undefined : h.indexOf('</section>', a));
  const starts = [...sec.matchAll(/<div class="peg-lc-group( peg-lc-empty)?">/g)];
  const slots = starts.map((x, i) => {
    const body = sec.slice(x.index, i + 1 < starts.length ? starts[i + 1].index : undefined);
    return { empty: !!x[1], lc: ((/class="peg-lcchip (?:ev|ot)">([^<]*)</.exec(body)) || [])[1] || '?', html: body };
  });
  const foot = (/<p class="peg-fine peg-lc-foot">([\s\S]*?)<\/p>/.exec(sec) || [])[1];
  return { sec, slots, noData: /Nothing to compare on/.test(sec),
           foot: foot == null ? null : foot.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim() };
}

/* What the community half must show for one relay answer: a card per LC
   with runs, a placeholder for the other slot, the no-data note only when
   neither has runs. Returns a list of failures (empty = fine). */
function lcExpectations(h, d, fam) {
  const c = lcCommunity(h);
  const errs = [];
  const grp = (lc) => ((d && d.groups) || []).find(x => x && x.lc === lc);
  const has = (lc) => { const g = grp(lc); return !!(g && g.n_runs > 0); };
  const ev = has('evosep'), ot = has('other');
  /* React's server renderer separates adjacent text nodes with <!-- -->:
     drop those outright, or "3 lab" + "s" reads "3 lab s". */
  const txt = (x) => x.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
  if (!ev && !ot) {
    if (!c.noData) errs.push('no-data family lost its empty state');
    if (c.slots.length) errs.push(`no-data family drew ${c.slots.length} LC card(s)`);
    return { c, errs, kind: 'none' };
  }
  if (c.noData) errs.push('a family with data still shows "Nothing to compare"');
  const order = c.slots.map(s => `${s.lc}${s.empty ? ' (empty)' : ''}`).join(' | ');
  const want = `Evosep${ev ? '' : ' (empty)'} | Other LC${ot ? '' : ' (empty)'}`;
  if (order !== want) errs.push(`slots "${order}", want "${want}"`);
  const tims = String(fam).toLowerCase() === 'timstof';
  for (const s of c.slots) {
    const t = txt(s.html);
    if (s.empty) {
      /* weekly spans 26 weeks, n_runs 90 days: older runs on this side mean
         "none lately", not "no lab yet" (the relay page says the same). */
      const g = grp(s.lc === 'Evosep' ? 'evosep' : 'other') || {};
      const older = (g.weekly || []).some(x => x != null && isFinite(x));
      const on = s.lc === 'Evosep' ? 'on an Evosep' : 'on a non-Evosep LC';
      if (older) {
        if (!/none in the last 90 days/.test(t) || /no lab yet/.test(t)) errs.push(`${s.lc} placeholder with older runs does not say "none in the last 90 days"`);
        if (!t.includes(`No ${fam} lab ${on} has shared PEG in the last 90 days.`)) errs.push(`${s.lc} placeholder with older runs lacks its lead sentence: ${t.trim()}`);
      } else if (!/no lab yet/.test(t) || /none in the last/.test(t)) errs.push(`${s.lc} placeholder does not say "no lab yet"`);
      if (!/peg_share: true/.test(t) || !/stan peg-sync/.test(t)) errs.push(`${s.lc} placeholder does not say how to join`);
      const how = s.lc === 'Evosep' ? 'on an Evosep' : tims ? 'with a nanoElute or other LC' : 'with a non-Evosep LC';
      if (!t.includes(`Labs running ${fam} ${how} can join`)) errs.push(`${s.lc} placeholder does not say "Labs running ${fam} ${how} can join": ${t.trim()}`);
      if (/\d%|peg-spark|peg-rng/.test(s.html)) errs.push(`${s.lc} placeholder shows a number or chart`);
    } else {
      const g = grp(s.lc === 'Evosep' ? 'evosep' : 'other') || {};
      const weeks = (g.weekly || []).filter(x => x != null && isFinite(x)).length;
      const want = [['median PEG share', /median PEG share/, t], ['p25–p75', /p25–p75/, t], ['clean', /Clean/, t],
                    ['labs · runs', /\d+ labs? · [\d,]+ runs/, t], ['p25–p75 bar', /class="peg-rng-iqr"/, s.html]];
      // PegSpark draws a dash, not a line, below two weeks of data.
      if (weeks >= 2) want.push(['weekly sparkline', /class="peg-spark/, s.html]);
      for (const [what, re, where] of want) if (!re.test(where)) errs.push(`${s.lc} card has no ${what}`);
    }
  }
  const one = ev !== ot;
  if (one) {
    const want = `Only ${fam} labs are compared here, since PEG share also depends on the detector; the ${ev ? 'Other LC' : 'Evosep'} side fills in as labs join.`;
    if (c.foot !== want) errs.push(`one-sided footnote is "${c.foot}", want "${want}"`);
  } else if (c.foot != null) errs.push('two-sided family grew the one-sided footnote');
  return { c, errs, kind: one ? 'one' : 'both' };
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
    /* Canonical on both sides, as the page compares (pegCanonName). */
    const canon = E.pegCanonName || (v => v);
    const me = canon(ov.sharing && ov.sharing.display_name);
    const meRanked = !!me && (fx.lb.ranked || []).some(r => canon(r.display_name) === me);
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
    /* v1.2.4: a family with runs on one LC shows that card beside an empty
       slot; only a family with no runs at all keeps the empty note. */
    const lcTab = lcExpectations(out, fx.lcOne, fam);
    expect(`LC comparison in the full tab (${lcTab.kind})`, !lcTab.errs.length, lcTab.errs.join('; '));
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

  /* 6. LC comparison with both groups present: unchanged by v1.2.4. */
  const lcProps = { labLc: ov.lab_lc || [], fam, onFam: () => {}, relayBase: relay };
  const both = renderQuiet('PegLcView', Object.assign({}, lcProps, { data: fx.lcBoth }));
  if (both !== null) {
    rendered++;
    const g = (both.match(/class="peg-lc-group"/g) || []).length;
    const r = lcExpectations(both, fx.lcBoth, fam);
    if (expect('both LC groups side by side', g === 2, `${g} groups`)
        & expect('two groups: two data cards, no placeholder, no footnote', r.kind === 'both' && !r.errs.length, r.errs.join('; ')))
      console.log('ok    PegLcView           Evosep + other LC groups');
  }

  /* 6b. v1.2.4. Brett's screenshot (2026-09-29): every family on the live
     relay has one LC side only -- UC Davis runs Evosep on its timsTOF and
     custom LC on its Orbitraps -- so the panel was one empty note. The side
     with runs is now its full card, the other a placeholder of the same
     footprint; only a family nobody shares keeps the empty note. Each
     answer is rendered as the relay gives it, under its own family. */
  /* A side with runs older than the window and none inside it. */
  const lcStale = JSON.parse(JSON.stringify(fx.lcOne));
  const staleOther = lcStale.groups.find(g => g.lc === 'other');
  staleOther.weekly = staleOther.weekly.length ? staleOther.weekly.map((v, i) => (i < 4 ? 0.3 + i / 10 : null))
                                               : [0.3, 0.4, 0.5, 0.6].concat(Array(22).fill(null));
  const lcCases = [['timsTOF, Evosep labs only', fx.lcOne], ['Orbitrap, other-LC labs only', fx.lcOther],
                   ['no lab shares this family', fx.lcNone], ['other LC with older runs only', lcStale]];
  for (const [label, d] of lcCases) {
    const dfam = (d && d.family) || fam;
    const h = renderQuiet('PegLcView', Object.assign({}, lcProps, { fam: dfam, data: d }));
    if (h === null) continue;
    rendered++;
    const r = lcExpectations(h, d, dfam);
    const tx = text(h).replace(/\s+/g, ' ');
    let ok = expect(`LC ${label}: community half`, !r.errs.length, r.errs.join('; '));
    ok &= expect(`LC ${label}: no NaN / undefined`, !/\bNaN\b|\bundefined\b|\[object Object\]/.test(tx));
    /* The lab half and its caveats are not this change's business: they
       must read the same whatever the relay says. */
    const labHalf = (x) => x.slice(0, x.indexOf('class="peg-lc-sep"'));
    ok &= expect(`LC ${label}: "Your instruments" half unchanged`, labHalf(h) === labHalf(both));
    /* No cross-family side by side: every card and slot is this family. */
    ok &= expect(`LC ${label}: at most one card per LC`, r.c.slots.length <= 2, `${r.c.slots.length} slots`);
    /* Off a timsTOF family the cards' clean rate is a timsTOF-calibrated
       class count, and says so (v1.2.1); on a timsTOF it carries no tag. */
    const tims = String(dfam).toLowerCase() === 'timstof';
    const calTags = (r.c.sec.match(/class="peg-caltag"/g) || []).length;
    const cleanCards = ((d && d.groups) || []).filter(g => g && g.n_runs > 0 && g.clean_pct != null).length;
    ok &= expect(`LC ${label}: clean rate tagged timsTOF-calibrated off a timsTOF`, calTags === (tims ? 0 : cleanCards), `${calTags} tag(s), ${cleanCards} clean card(s)`);
    if (ok) {
      const desc = r.c.slots.length ? r.c.slots.map(s => `${s.lc}${s.empty ? ' placeholder' : ' card'}`).join(' + ') : 'empty state';
      console.log(`ok    PegLcView           ${label} (${dfam}): ${desc}${calTags ? ' · clean tagged timsTOF-calibrated' : ''}`);
    }
  }
  /* The family name reaches the placeholder and the footnote as text, and
     a relay can name any family: it must never become markup. */
  const evil = '<img src=x onerror=alert(1)>';
  const hostileFam = renderQuiet('PegLcView', Object.assign({}, lcProps, { fam: evil, data: Object.assign({}, fx.lcOne, { family: evil }) }));
  if (hostileFam !== null) {
    rendered++;
    if (expect('LC family name escaped in the placeholder and footnote', !hostileFam.includes(evil)
               && (hostileFam.match(/&lt;img src=x onerror=alert\(1\)&gt;/g) || []).length >= 3))
      console.log('ok    PegLcView           hostile family name rendered as text');
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

  /* RG-UI-2. A lab with one Exploris on Evosep and one on a custom LC,
     viewing the custom one: the family's Evosep board lists this lab and
     nobody else. "No Exploris lab on Evosep has shared" over its own YOU
     row is false, and contradicts the header note ("your lab is, through
     another instrument") right above it. */
  const me2 = 'E2E Lab';
  const selfRow = Object.assign({}, (fx.lb.ranked || [])[0] || {}, { display_name: me2, rank: 1, verified: true, n_runs: 12 });
  const selfOnly = Object.assign({}, fx.lb, { family: 'Exploris', spd: 60, ranked: [selfRow], unranked: [] });
  const explView = { ov: Object.assign({}, expl, { sharing: { enabled: true, source: 'config', display_name: me2 } }),
                     family: 'Exploris', isEvosep: false, chips: ['60'], coh: '60', win: '30', onCoh: () => {}, onWin: () => {},
                     stale: false, error: null, myName: me2, relayBase: relay };
  const onlyMe = renderQuiet('PegBoardView', Object.assign({}, explView, { data: selfOnly }));
  const onlyMeUnranked = renderQuiet('PegBoardView', Object.assign({}, explView, {
    data: Object.assign({}, selfOnly, { ranked: [], unranked: [{ display_name: me2, n_runs: 2, verified: true }] }) }));
  const nobody = renderQuiet('PegBoardView', Object.assign({}, explView, {
    data: Object.assign({}, fx.lbEmpty, { family: 'Exploris', spd: 60 }) }));
  if (onlyMe !== null && onlyMeUnranked !== null && nobody !== null) {
    rendered += 3;
    const [a, b, c] = [onlyMe, onlyMeUnranked, nobody].map(h => text(h).replace(/\s+/g, ' '));
    if (expect('board listing only this lab: "No other Exploris lab on Evosep"', /No other Exploris lab on Evosep has shared 60 SPD/.test(a), a.slice(0, 400))
        & expect('...never "No Exploris lab" over its own YOU row', !/No Exploris lab on Evosep/.test(a) && /<tr class="peg-you"/.test(onlyMe))
        & expect('...same when this lab is only unranked there', /No other Exploris lab on Evosep has shared/.test(b) && !/No Exploris lab on Evosep/.test(b))
        & expect('empty board still says no Exploris lab on Evosep shared', /No Exploris lab on Evosep has shared 60 SPD/.test(c) && !/No other/.test(c)))
      console.log('ok    PegBoardView        non-Evosep: board of only this lab says "No other"');
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
    /* Only the lab half: since v1.2.4 the community half draws an "Other
       LC" placeholder for a family with Evosep labs only. */
    const labHalf = nullLc.slice(0, nullLc.indexOf('class="peg-lc-sep"'));
    if (expect('null LC is not an "Other LC"', !/>Other LC</.test(labHalf) && /peg-lcchip nr/.test(labHalf))
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

  /* 9. Review round 1 leftovers: canonical lab names (F1), sharing as the
     relay sees it (F2/F10), case-blind family matching (F4). */
  checkPegNamesAndSharing(fx, { relay, fam, seed, expect, text, boardProps, lcProps });

  /* 10. v1.2.1: the score's classes are timsTOF-calibrated. */
  checkPegCalibration(fx, { seed, expect, text });
  E.PEG_CACHE.clear();
}

/* v1.2.1. The 0-100 PEG score and its classes are calibrated on timsTOF
   data, and the two vendors' readers put intensities on different scales:
   UC Davis's timsTOF records ~3 PEG ions a run to the Orbitraps' ~27 at the
   same 1e4 floor, so the live tab read 15% clean on the timsTOF against 1%
   on Orbitraps carrying 25-35x less PEG (2026-09-29). Until the
   measurement is fixed the tab must not set classes side by side across
   families: no clean column in a multi-family "Your instruments" table,
   class elements labelled off a timsTOF, and no clean rate in that lede.
   Rendered once per overview in fx.views -- the live UC Davis timsTOF HT,
   Exploris 480 and Lumos documents when --peg points at them. */
function checkPegCalibration(fx, { seed, expect, text }) {
  const E = exported;
  const views = fx.views || [{ name: 'peg_overview.json', ov: fx.ov }];
  const famKey = (f) => String(f == null ? '' : f).trim().toLowerCase();
  const kinds = { tims: 0, off: 0 };
  const sectionOf = (h, head) => { const a = h.indexOf(head); return a < 0 ? '' : h.slice(a, h.indexOf('</section>', a)); };
  for (const { name, ov: v } of views) {
    const vfam = v.instrument_family || '';
    const tims = famKey(vfam) === 'timstof';
    const relay = ((v.sharing && v.sharing.relay_url) || E.PEG_RELAY_DEFAULT).replace(/\/+$/, '');
    E.PEG_CACHE.clear();
    seed('/api/peg/overview', v);
    /* The LC answer this family gets from the live relay today: Evosep
       labs only on a timsTOF, other-LC labs only on an Orbitrap. */
    const lcFx = tims ? fx.lcOne : Object.assign({}, fx.lcOther, { family: vfam });
    if (vfam) {
      for (const spd of ['100', '60', '30']) {
        for (const win of ['30', '90', '365']) seed(E.pegBoardUrl(relay, vfam, spd, win), fx.lbEmpty);
        seed(E.pegTrendUrl(relay, vfam, spd), fx.trend);
      }
      seed(E.pegLcUrl(relay, vfam), lcFx);
    }
    const h = renderQuiet('PegTab', {});
    if (h === null) continue;
    rendered++;
    if (!(v.runs || []).length) { console.log(`skip  PegTab              ${name}: no PEG runs, nothing class-based on screen`); continue; }
    kinds[tims ? 'tims' : 'off']++;
    const who = `${name} (${v.instrument}, ${vfam || 'no family'})`;
    const tx = text(h);
    let ok = expect(`${who}: no NaN / undefined on screen`, !/\bNaN\b|\bundefined\b|\[object Object\]/.test(tx));

    /* The note, and the tag on each class-based element. The community LC
       cards' clean rate is one more since v1.2.4, counted on its own. */
    const lcCom = vfam ? lcExpectations(h, lcFx, vfam) : null;
    const lcTags = lcCom ? (lcCom.c.sec.match(/class="peg-caltag"/g) || []).length : 0;
    const lcCleanCards = vfam ? (lcFx.groups || []).filter(g => g && g.n_runs > 0 && g.clean_pct != null).length : 0;
    const tags = (h.match(/class="peg-caltag"/g) || []).length - lcTags;
    const tileAt = h.indexOf('Clean QCs · 30 days');
    const cleanTile = tileAt < 0 ? '' : h.slice(tileAt, h.indexOf('class="peg-tile"', tileAt));
    const tl = sectionOf(h, 'PEG over time'), legend = tl.slice(tl.indexOf('class="peg-legend"'));
    const cal = sectionOf(h, 'Every QC day since'), calMeta = cal.slice(cal.indexOf('class="peg-cal-meta"'));
    if (tims) {
      ok &= expect(`${who}: no timsTOF-calibrated note on a timsTOF`, !/peg-calnote/.test(h) && !/timsTOF-calibrated/.test(tx), `${tags} tag(s)`);
    } else {
      ok &= expect(`${who}: one-line calibration note`, (h.match(/class="peg-fine peg-calnote"/g) || []).length === 1
                   && /calibrated on timsTOF data; on this instrument, compare PEG share/.test(tx));
      ok &= expect(`${who}: Clean QCs tile labelled timsTOF-calibrated`, /peg-caltag[^>]*>timsTOF-calibrated</.test(cleanTile), cleanTile.slice(0, 200));
      ok &= expect(`${who}: timeline class legend labelled`, legend.length > 0 && /peg-caltag[^>]*>timsTOF-calibrated</.test(legend));
      ok &= expect(`${who}: calendar legend labelled`, calMeta.length > 0 && /peg-caltag[^>]*>timsTOF-calibrated</.test(calMeta));
      ok &= expect(`${who}: tags only on class-based elements (2 tiles, 2 legends)`, tags === 4, `${tags} tags`);
      ok &= expect(`${who}: community LC clean rate tagged too`, lcTags === lcCleanCards, `${lcTags} tag(s), ${lcCleanCards} clean card(s)`);
    }
    /* v1.2.4: this family's LC answer, as the live relay gives it, is its
       card beside an empty slot -- never the empty note. */
    if (lcCom) ok &= expect(`${who}: community LC half`, !lcCom.errs.length, lcCom.errs.join('; '));

    /* The hero lede: the clean rate of the best 90 days goes, the rest stays. */
    const lede = text((/<p class="peg-lede">([\s\S]*?)<\/p>/.exec(h) || [])[1] || '').replace(/\s+/g, ' ');
    const s = v.summary || {}, B = v.baseline;
    const vsBest = !!(s.n_30d && B && B.median_pct != null);
    if (tims) {
      if (vsBest) ok &= expect(`${who}: timsTOF lede still quotes the best-90-day clean rate (control)`, /% of QCs came back clean/.test(lede), lede);
    } else {
      ok &= expect(`${who}: lede does not quote a clean rate`, !/came back clean/.test(lede), lede);
      if (vsBest) ok &= expect(`${who}: lede keeps the best-90-day comparison`, /your best 90 days \(/.test(lede), lede);
    }

    /* "Your instruments": no class-based column once the rows span families. */
    const lab = v.lab_lc || [];
    const multi = new Set(lab.map(x => famKey(x.family)).filter(Boolean)).size > 1;
    const lc = sectionOf(h, 'Evosep vs other LC');
    const hd = text((/<div class="peg-lc-row peg-hd">([\s\S]*?)<\/div>/.exec(lc) || [])[1] || '');
    if (lab.length) {
      const shares = (lc.match(/class="peg-val"/g) || []).length, sparks = (lc.match(/class="peg-spark/g) || []).length;
      if (multi) {
        ok &= expect(`${who}: multi-family LC rows have no clean column`, !/\bclean\b/i.test(hd) && !/class="peg-m"/.test(lc) && /peg-lc-rows peg-noclean/.test(lc), hd);
        ok &= expect(`${who}: ...and keep median PEG share and the 26-week line`, /median PEG share/.test(hd) && /26 weeks/.test(hd)
                     && shares === lab.length, `${shares} shares for ${lab.length} rows, ${sparks} sparklines`);
        ok &= expect(`${who}: ...and the caveat says the classes are timsTOF-calibrated`,
                     /calibrated on timsTOF data and are not comparable across instrument families/.test(text(lc).replace(/\s+/g, ' ')));
      } else {
        ok &= expect(`${who}: single-family LC rows keep the clean column`, /\bclean\b/i.test(hd) && (lc.match(/class="peg-m"/g) || []).length === lab.length, hd);
      }
    }
    if (ok) {
      const slots = lcCom && lcCom.c.slots.length ? ` · community ${lcCom.c.slots.map(s => `${s.lc}${s.empty ? ' placeholder' : ' card'}`).join(' + ')}` : '';
      console.log(`ok    PegTab              ${name}: ${vfam}${tims ? ' · no calibration labels' : ` · note + ${tags} timsTOF-calibrated tags · lede without a clean rate`}` +
                  `${lab.length ? (multi ? ` · ${lab.length} LC rows, ${new Set(lab.map(x => famKey(x.family))).size} families, no clean column` : ' · LC clean column kept') : ''}${slots}`);
    }
  }
  if (!kinds.off) console.log('note  no non-timsTOF overview among the fixtures: the calibration labels were not exercised');
  if (!kinds.tims) console.log('note  no timsTOF overview among the fixtures: the unlabelled control was not exercised');

  /* PegLcView on its own: one family keeps its clean column; two custom-LC
     families (no Evosep split, so no "Different instrument families"
     headline) still lose it, and still say why. */
  const row = (instrument, family, lc_system, clean) => ({ instrument, family, lc_system, n_90d: 30, median_90d: 0.4,
    clean_rate_90d: clean, n_365d: 100, median_365d: 0.5, weekly: [0.2, 0.3, 0.4] });
  const lcBase = { fam: 'timsTOF', onFam: () => {}, relayBase: E.PEG_RELAY_DEFAULT, data: fx.lcOne };
  const one = renderQuiet('PegLcView', Object.assign({}, lcBase, { labLc: [row('timsTOF HT', 'timsTOF', 'evosep', 15), row('timsTOF Ultra', 'timsTOF', 'custom', 30)] }));
  const two = renderQuiet('PegLcView', Object.assign({}, lcBase, { labLc: [row('Orbitrap Fusion Lumos', 'Lumos', 'custom', 1), row('Orbitrap Exploris 480', 'Exploris', 'custom', 1)] }));
  if (one !== null && two !== null) {
    rendered += 2;
    const hdOf = (h) => text((/<div class="peg-lc-row peg-hd">([\s\S]*?)<\/div>/.exec(h) || [])[1] || '');
    if (expect('one family on two LCs: clean column kept, no calibration caveat',
               /\bclean\b/.test(hdOf(one)) && (one.match(/class="peg-m"/g) || []).length === 2 && !/calibrated on timsTOF/.test(one), hdOf(one))
        & expect('two custom-LC families: no clean column, caveat without the Evosep headline',
                 !/\bclean\b/.test(hdOf(two)) && !/class="peg-m"/.test(two) && /calibrated on timsTOF data/.test(text(two))
                 && !/Different instrument families/.test(two), hdOf(two)))
      console.log('ok    PegLcView           one family keeps clean; Lumos + Exploris drop it and say why');
  }
}

/* The relay's own answers, computed with _clean_text from hf_space/app.py
   (extracted through the AST and run on Python 3.13, unidata 15.1). If the
   relay's rule changes, regenerate these from it -- do not edit them to
   match the page. */
const RELAY_CLEAN_TEXT = [
  ['E2E  Lab', 'E2E Lab'], [' E2E\tLab \n', 'E2E Lab'], ['E2E\u200bLab', 'E2ELab'], ['E2E\u2800Lab', 'E2E Lab'],
  ['\uff25\uff12\uff25 Lab', 'E2E Lab'], ['Proteo\u0302mica Lab', 'Prote\u00f4mica Lab'], ['Cafe\u034f\u0301 Lab', 'Caf\u00e9 Lab'],
  ['\u202eLab', 'Lab'], ['Lab\u001cX', 'Lab X'], ['A\u0085B', 'A B'], ['A\ufeffB', 'AB'], ['\ufb01ne Lab', 'fine Lab'],
  ['Clogged PeakTail', 'Clogged PeakTail'], ['A\u3164B', 'AB'], ['A\ufe0fB', 'AB'], ['A\u00a0\u3000B', 'A B'],
  ['A\u180eB', 'AB'], ['A\u0000B\u007f', 'AB'], ['A\udb40\udc41B', 'AB'], ['\u2800', ''], ['UC\u2003Davis\u2029Core', 'UC Davis Core'],
];

function checkPegNamesAndSharing(fx, { relay, fam, seed, expect, text, boardProps, lcProps }) {
  const E = exported;
  if (!E.pegCanonName || !E.PegShare) {
    console.error('FAIL  PEG: page does not define pegCanonName / PegShare'); fails++; return;
  }
  const ov = fx.ov;
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const q = (s) => JSON.stringify(s).replace(/[\u0080-￿]/g, c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));

  /* F1. The page must canonicalise exactly as the relay does, or a name the
     relay treats as the lab's is not the lab's here. */
  const off = RELAY_CLEAN_TEXT.filter(([i, o]) => E.pegCanonName(i) !== o);
  off.forEach(([i, o]) => expect(`pegCanonName(${q(i)}) matches the relay`, false, `got ${q(E.pegCanonName(i))}, relay ${q(o)}`));
  if (!off.length & expect('pegCanonName of a non-string is empty', E.pegCanonName(null) === '' && E.pegCanonName(5) === ''))
    console.log(`ok    pegCanonName        ${RELAY_CLEAN_TEXT.length} cases agree with the relay's _clean_text`);

  /* The same lab name as community.yml might hold it: NFD accents, a
     leading braille blank, zero-width spaces and doubled blanks. */
  const mangle = (n) => '\u2800' + n.normalize('NFD').split(' ').join(' \u200b\u00a0') + '\ufeff';
  const ranked = (fx.lb.ranked || []).filter(r => !/[<>&]/.test(r.display_name));
  const unranked = fx.lb.unranked || [];
  if (ranked.length) {
    const who = ranked[0].display_name;
    const yb = renderQuiet('PegBoardView', Object.assign({}, boardProps, { data: fx.lb, myName: mangle(who) }));
    const nb = renderQuiet('PegBoardView', Object.assign({}, boardProps, { data: fx.lb, myName: who.toLowerCase() === who ? who.toUpperCase() : who.toLowerCase() }));
    if (yb !== null && nb !== null) {
      rendered += 2;
      const you = (h) => ((/<tr class="peg-you"[\s\S]*?<span class="peg-labn">([^<]*)</.exec(h) || [])[1]);
      const un = unranked.length ? renderQuiet('PegBoardView', Object.assign({}, boardProps, { data: fx.lb, myName: mangle(unranked[0].display_name) })) : '';
      if (un) rendered++;
      if (expect('YOU row found through a non-canonical local name', you(yb) === esc(who), `YOU on ${you(yb)}`)
          & expect('exactly one YOU row', (yb.match(/<tr class="peg-you"/g) || []).length === 1)
          & expect('case still tells labs apart (the relay keeps case)', !/<tr class="peg-you"/.test(nb))
          & expect('unranked lab found through a non-canonical local name',
                   !unranked.length || text(un).includes(`${unranked[0].display_name} (you)`)))
        console.log(`ok    PegBoardView        YOU through NFD / zero-width / doubled blanks: "${who}"`);
    }
  }

  /* F2 / F10, narrowed by RG-UI-1. With no setting on this host (the
     hosted dashboard: source 'off') the relay's board speaks for the lab,
     and only as a past fact -- a row on the board says runs were sent in
     the window, not that anything is being sent now. An explicit opt-out
     here (source 'opted_out') is never overridden by the board: the relay
     keeps the runs sent before it for the whole window. */
  const share = (sharing, onBoard, boardWin) => renderQuiet('PegShare', { sharing, myName: E.pegCanonName(sharing.display_name), onBoard, boardWin });
  const S = {
    seen: share({ enabled: false, display_name: 'E2E Lab', source: 'off' }, true, '30'),
    seenNoSource: share({ enabled: false, display_name: 'E2E Lab' }, true, '30'),
    seenYear: share({ enabled: false, display_name: 'E2E Lab', source: 'off' }, true, '365'),
    optedSeen: share({ enabled: false, display_name: 'E2E Lab', source: 'opted_out' }, true, '30'),
    opted: share({ enabled: false, display_name: 'E2E Lab', source: 'opted_out' }, false),
    off: share({ enabled: false, display_name: 'E2E Lab' }, false),
    offSrc: share({ enabled: false, display_name: 'E2E Lab', source: 'off' }, false),
    env: share({ enabled: true, display_name: 'E2E Lab', source: 'env' }, false),
    cfg: share({ enabled: true, display_name: 'E2E Lab', source: 'config' }, false),
    old: share({ enabled: true, display_name: 'E2E Lab' }, false),
  };
  if (Object.values(S).every(h => h !== null)) {
    rendered += Object.keys(S).length;
    const T = Object.fromEntries(Object.entries(S).map(([k, h]) => [k, text(h).replace(/\s+/g, ' ')]));
    const isOn = (t) => /Sharing PEG with the community as E2E Lab/.test(t) && !/PEG stays in your lab/.test(t);
    const isOff = (k) => /PEG stays in your lab/.test(T[k]) && /peg-switch off/.test(S[k]) && !/Sharing PEG with the community/.test(T[k]);
    /* What the relay shows, as a past fact, never "sharing is on". */
    const seenOnly = (t) => /On the community board as E2E Lab/.test(t) && !/PEG stays in your lab/.test(t)
      && !/Sharing PEG with the community/.test(t) && !/turn (that|it) off there/.test(t);
    if (expect('no setting here, lab on the board -> what the board shows, not "sharing"', seenOnly(T.seen) && seenOnly(T.seenNoSource), T.seen.slice(-360))
        & expect('...worded as a past fact over the window the board covered',
                 /Runs from the last 30 days are on the board under this name/.test(T.seen)
                 && /Runs from the last 12 months are on the board under this name/.test(T.seenYear))
        & expect('...and says this dashboard is not what sends them', /does not send them/.test(T.seen))
        & expect('opted out here, lab still on the board -> OFF, earlier runs named as such', isOff('optedSeen')
                 && /set not to share/.test(T.optedSeen) && /still on the board/.test(T.optedSeen), T.optedSeen.slice(-360))
        & expect('opted out here, not on a board -> OFF, no board sentence', isOff('opted') && /set not to share/.test(T.opted)
                 && !/on the board under/.test(T.opted))
        & expect('not on a board and off here -> the OFF text', isOff('off') && isOff('offSrc'))
        & expect('source env -> names STAN_PEG_SHARE', isOn(T.env) && /STAN_PEG_SHARE/.test(T.env))
        & expect('source config, or none (older server) -> the local community.yml text',
                 [T.cfg, T.old].every(t => isOn(t) && /Each stan peg-sync sends your QC runs/.test(t) && !/STAN_PEG_SHARE/.test(t))))
      console.log('ok    PegShare            no setting: board as past fact; opted out: OFF; env/config/absent worded');
  }

  /* The same, end to end: this host says off, the relay's board lists the
     lab. The card and the rank tile both have to believe the board. */
  const lbName = ranked.length ? ranked[0].display_name : null;
  const unName = unranked.length ? unranked[0].display_name : null;
  const tab = (sharing, board = fx.lb) => {
    E.PEG_CACHE.clear();
    seed('/api/peg/overview', Object.assign({}, ov, { sharing: Object.assign({}, ov.sharing, sharing) }));
    for (const spd of ['100', '60', '30']) {
      for (const win of ['30', '90', '365']) seed(E.pegBoardUrl(relay, fam, spd, win), spd === String(fx.lb.spd) ? board : fx.lbEmpty);
    }
    seed(E.pegLcUrl(relay, fam), fx.lcOne);
    const h = renderQuiet('PegTab', {});
    if (h !== null) rendered++;
    return h;
  };
  const card = (h) => { const a = h.indexOf('What your lab shares'); return a < 0 ? '' : text(h.slice(a, h.indexOf('</section>', a))).replace(/\s+/g, ' '); };
  const cardHtml = (h) => { const a = h.indexOf('What your lab shares'); return a < 0 ? '' : h.slice(a, h.indexOf('</section>', a)); };
  const tile = (h) => text((/Community rank[\s\S]*?<div class="peg-tile-d">([\s\S]*?)<\/div>/.exec(h) || [])[1] || '');
  if (ov.lc_system === 'evosep' && lbName) {
    const a = tab({ enabled: false, source: 'off', display_name: mangle(lbName) });
    const b = unName ? tab({ enabled: false, source: 'off', display_name: mangle(unName) }) : '';
    const c = tab({ enabled: false, source: 'off', display_name: 'Nobody Shares Lab' });
    if (a && c) {
      if (expect('no setting here, lab ranked on the board -> card says so, as a past fact',
                 /Runs from the last 30 days are on the board under this name/.test(card(a)), card(a).slice(-240))
          & expect('...and the rank tile shows the rank', /Community rank[\s\S]{0,200}#\d/.test(a))
          & expect('no setting here, lab only unranked -> tile says not ranked yet, not "sharing is off"',
                   !b || (/not ranked yet/.test(tile(b)) && /on the board under this name/.test(card(b))), b ? tile(b) : '')
          & expect('no setting here, lab on no board -> OFF card and "sharing is off"',
                   /PEG stays in your lab/.test(card(c)) && /sharing is off/.test(tile(c)), `${card(c).slice(0, 80)} | ${tile(c)}`))
        console.log(`ok    PegTab              no setting here, relay lists "${lbName}"${unName ? ` / "${unName}"` : ''}: shown from the board`);
    }
    /* RG-UI-1. The same lab, opted out on this host (peg_share: false).
       Its pre-opt-out runs are still inside the relay's window, ranked or,
       once fewer than 5 are left, unranked. Neither makes it "sharing". */
    const canonLb = E.pegCanonName(lbName);
    const onlyUnranked = Object.assign({}, fx.lb, {
      ranked: (fx.lb.ranked || []).filter(r => E.pegCanonName(r.display_name) !== canonLb),
      unranked: [...(fx.lb.unranked || []), { display_name: lbName, n_runs: 2, verified: true }] });
    const d = tab({ enabled: false, source: 'opted_out', display_name: mangle(lbName) });
    const e = tab({ enabled: false, source: 'opted_out', display_name: mangle(lbName) }, onlyUnranked);
    if (d && e) {
      const offCard = (h) => /PEG stays in your lab/.test(card(h)) && /peg-switch off/.test(cardHtml(h))
        && !/Sharing PEG with the community/.test(card(h)) && !/On the community board as/.test(card(h));
      if (expect('opted out here, lab ranked on the board -> card OFF, earlier runs named', offCard(d) && /still on the board/.test(card(d)), card(d).slice(-300))
          & expect('opted out here, lab only unranked -> card OFF', offCard(e), card(e).slice(-300))
          & expect('...and the tile says "sharing is off", not "5 QCs needed"', /sharing is off/.test(tile(e)) && !/not ranked yet/.test(tile(e)), tile(e)))
        console.log(`ok    PegTab              opted out here, relay still lists "${lbName}": sharing shown OFF`);
    }
  }

  /* F4. Families are one family whatever their case. */
  const upper = (d) => Object.assign({}, d, { cohorts: (d.cohorts || []).map(c => Object.assign({}, c, { family: String(c.family).toUpperCase() })) });
  if (fam) {
    /* Board chips: an SPD the relay reports labs in, under another case. */
    E.PEG_CACHE.clear();
    const extra = { family: String(fam).toUpperCase(), spd: 200, n_labs: 2, n_runs_365d: 40 };
    const lbU = upper(Object.assign({}, fx.lb, { cohorts: [...(fx.lb.cohorts || []), extra] }));
    for (const spd of ['100', '60', '30', '200']) for (const win of ['30', '90', '365']) seed(E.pegBoardUrl(relay, fam, spd, win), lbU);
    const brd = renderQuiet('PegBoard', { ov, isEvosep: true, family: fam, relayBase: relay, defaultSpd: '100',
                                          myName: ov.sharing && ov.sharing.display_name, rankLb: { data: lbU, error: null } });
    if (brd !== null) {
      rendered++;
      if (expect('cohort chips found under another case', new RegExp(`>${fam} · 200 SPD<`).test(brd), 'no 200 SPD chip'))
        console.log(`ok    PegBoard            relay family "${extra.family}" matches "${fam}"`);
    }
    /* Timeline band: its n_labs lookup reads the rank board's cohorts. The
       trend is forced to 3+ labs a week, since the band skips thinner weeks
       and the real UC Davis trend has one lab. */
    const trend3 = { weeks: ((fx.trend && fx.trend.weeks) || []).map(w => Object.assign({}, w, {
      n_labs: Math.max(3, w.n_labs || 0), p25: w.p25 == null ? 0.1 : w.p25, p75: w.p75 == null ? 2 : w.p75 })) };
    const orig = windowStub.localStorage.getItem;
    windowStub.localStorage.getItem = (k) => (k === 'stan.peg.spd' ? '100' : null);
    try {
      const band = (lb) => {
        E.PEG_CACHE.clear();
        seed('/api/peg/overview', ov);
        for (const spd of ['100', '60', '30']) {
          for (const win of ['30', '90', '365']) seed(E.pegBoardUrl(relay, fam, spd, win), lb);
          seed(E.pegTrendUrl(relay, fam, spd), trend3);
        }
        seed(E.pegLcUrl(relay, fam), fx.lcOne);
        const h = renderQuiet('PegTab', {});
        if (h !== null) rendered++;
        return h && /Community p25–p75 · /.test(h);
      };
      const three = Object.assign({}, fx.lb, { cohorts: [{ family: fam, spd: 100, n_labs: 4, n_runs_365d: 400 }] });
      if (ov.lc_system === 'evosep' && (ov.rolling || {})['100']) {
        const same = band(three), diff = band(upper(three));
        if (expect('timeline band drawn when the relay spells the family as we do (control)', same)
            & expect('timeline band drawn when the relay spells it in another case', diff))
          console.log('ok    PegTimeline         community band found under another family case');
      }
    } finally {
      windowStub.localStorage.getItem = orig;
    }
    /* LC panel: one family option per family, whatever the relay's case. */
    const lcU = Object.assign({}, fx.lcOne, { families: [...(fx.lcOne.families || []),
      { family: String(fam).toUpperCase(), evosep_runs: 5, other_runs: 5, evosep_labs: 1, other_labs: 1 },
      { family: String(fam).toLowerCase(), evosep_runs: 5, other_runs: 0, evosep_labs: 1, other_labs: 0 }] });
    const lcv = renderQuiet('PegLcView', Object.assign({}, lcProps, { data: lcU }));
    if (lcv !== null) {
      rendered++;
      const grp = (/<div class="peg-seg" role="group" aria-label="Instrument family">([\s\S]*?)<\/div>/.exec(lcv) || [])[1] || '';
      const opts = [...grp.matchAll(/<button[^>]*>([^<]*)<\/button>/g)].map(x => x[1]);
      const same = opts.filter(o => o.toLowerCase() === String(fam).toLowerCase());
      if (expect('one family option per family, whatever its case', same.length === 1, opts.join(', '))
          & expect('that option is the one pressed', new RegExp(`aria-pressed="true"[^>]*>${fam}<`).test(grp)))
        console.log(`ok    PegLcView           family options: ${opts.join(', ')}`);
    }
  }
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
