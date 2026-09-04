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
 * browser stubs, and server-render the Evosep charts against a real
 * evosep_column_health.json for every window the UI offers.
 *
 *     npm install --no-save @babel/standalone react react-dom
 *     node scripts/render_check.js [path/to/evosep_column_health.json]
 *
 * Exit 0 = every component rendered. Exit 1 = one threw, with the stack.
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

const names = ['EvBaselineChart', 'evBaselineSkeleton', 'EvWashFlow', 'EvColumnLifetimes', 'EvColumnAging'];
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

const docPath = process.argv[2] || '/tmp/ev_full.json';
if (!fs.existsSync(docPath)) {
  console.error(`no API document at ${docPath} — pass one as argv[1]`);
  process.exit(2);
}
const doc = JSON.parse(fs.readFileSync(docPath, 'utf8'));
const flagsByRun = {};
(doc.flags || []).forEach(f => { flagsByRun[f.start] = f; });
const analytical = Object.values(doc.methods || {}).filter(m2 => m2.analytical);
if (!analytical.length) { console.error('document has no analytical methods'); process.exit(2); }

/* Every window the UI offers, including the 'This column' derivation. */
const colDays = Math.max(14, Math.ceil((doc.column && doc.column.days_since || 0) + 3));
const WINDOWS = [['This column', colDays], ['90 days', 90], ['1 year', 365], ['All', 0]];

let fails = 0, rendered = 0;
for (const [label, sinceDays] of WINDOWS) {
  for (const ms of analytical) {
    let out;
    try {
      out = ReactDOMServer.renderToStaticMarkup(
        React.createElement(exported.EvBaselineChart, {
          method: ms.method, ms, hue: '#60a5fa', flagsByRun, sinceDays,
          installedAt: doc.column && doc.column.installed,
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
function render(name, props) {
  try {
    return ReactDOMServer.renderToStaticMarkup(React.createElement(exported[name], props));
  } catch (e) {
    console.error(`FAIL  ${name}: ${e.message}`);
    console.error(e.stack.split('\n').slice(0, 4).join('\n'));
    fails++; return null;
  }
}

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
      if (tile.includes('\u2014')) {
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

console.log(`\n${rendered} render(s), ${fails} failure(s)`);
process.exit(fails ? 1 : 0);
