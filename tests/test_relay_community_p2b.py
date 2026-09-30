"""Community site redesign, phase P2b (the filter bar and the lab trend), in the vendored relay.

Spec: docs/superpowers/specs/2026-09-29-community-redesign-and-precursor-lookup-design.md
(§A.1 item 3, §A.2, §A.3 B2 and B3, §A.6 P2); mockup: docs/community-redesign/mockup/
(v3.1, approved 2026-09-29).

Server side: SPACE_VERSION 1.4.0. Page side, as text: the sticky bar's markup
and CSS, its phone fold, the scroll padding that keeps anchors clear of it,
and TIC overlay and PEG Watch byte-identical to main 54c4671. Page side, run in
node against the page's own script (a harness like the one in
tests/test_relay_community_p1.py, with a class list, attributes, a root
element and browser storage that throws): the one cohort key (B2), the one
filter state driving every panel and re-rendering only the panels that follow
a changed field, the facet cascade, the per-chart amount selects and the
DIA / DDA / All tabs as views of the state, the 400 px summary, the lab trend's
cohort matching and reference (B3), escaping of hostile names in the bar and
the trend, and no browser storage.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from tests.test_relay_community_p1 import _dda, _main_script, _row
from tests.test_relay_peg import NODE, _page, client, hub, needs_node, relay  # noqa: F401  (fixtures)

EVIL = '<img src=x onerror="alert(1)">'

# ── harness ──────────────────────────────────────────────────────────

_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = fs.readFileSync(process.argv[3], 'utf8');
// One persistent element per id, with a working class list and attributes;
// a <select> keeps its options and falls back to the first, as a browser does.
class ClassList {
    constructor() { this.s = new Set(); }
    add(...c) { c.forEach(x => this.s.add(x)); }
    remove(...c) { c.forEach(x => this.s.delete(x)); }
    contains(c) { return this.s.has(c); }
    toggle(c, f) { const on = f === undefined ? !this.s.has(c) : !!f; if (on) this.s.add(c); else this.s.delete(c); return on; }
}
class El {
    constructor(id) {
        this.id = id; this.style = {}; this.textContent = ''; this.value = ''; this.dataset = {};
        this.disabled = false; this.checked = false; this.hidden = false; this.options = []; this._html = '';
        this.classList = new ClassList(); this.attrs = {}; this.offsetHeight = 0;
    }
    get innerHTML() { return this._html; }
    set innerHTML(h) {
        this._html = String(h);
        const vals = [...this._html.matchAll(/<option(?: value="([^"]*)")?[^>]*>([^<]*)<\/option>/g)]
            .map(m => (m[1] !== undefined ? m[1] : m[2]));
        this.options = vals.map(v => ({ value: v }));
        if (vals.length && !vals.includes(this.value)) this.value = vals[0];
    }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
    addEventListener() {} appendChild() {}
    querySelector() { return null; } querySelectorAll() { return []; }
}
const els = {};
const byId = (id) => (els[id] = els[id] || new El(id));
const plots = [];
// Elements a selector returns, filled by a scenario (tabs, mode buttons).
const qsa = {};
const errors = [];
const con = { log() {}, info() {}, warn() {}, error: (...a) => errors.push(a.map(x => (x && x.stack) || String(x)).join(' ')) };
const root = { style: { props: {}, setProperty(k, v) { this.props[k] = v; } } };
const blocked = () => { throw new Error('SecurityError: browser storage is blocked here'); };
const win = { addEventListener() {}, innerWidth: 1280 };
['localStorage', 'sessionStorage'].forEach(k => Object.defineProperty(win, k, { get: blocked }));
const ctx = vm.createContext({
    console: con, els, plots, qsa, root, El,
    document: { getElementById: byId, querySelector: () => null, querySelectorAll: (s) => qsa[s] || [],
                addEventListener() {}, createElement: () => new El(''), body: new El('body'), documentElement: root },
    window: win,
    fetch: () => new Promise(() => {}),       // the page's own load never settles here
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    Plotly: { newPlot: (id, traces, layout) => plots.push({ id: typeof id === 'string' ? id : id.id, traces, layout }),
              Plots: { resize() {} }, purge() {} },
});
// Storage that throws, as in a private window with site data blocked: the
// page must neither need it nor crash on it.
['localStorage', 'sessionStorage'].forEach(k => Object.defineProperty(ctx, k, { get: blocked }));
vm.runInContext(src, ctx);
const out = vm.runInContext(scenario, ctx);
process.stdout.write(JSON.stringify({ out, errors }));
process.exit(0);
"""


def _run(client, tmp_path: Path, scenario: str):
    html = _page(client)
    esc_block = re.search(r'<script id="stan-esc">(.*?)</script>', html, re.S).group(1)
    (tmp_path / "main.js").write_text(esc_block + "\n" + _main_script(html))
    (tmp_path / "scenario.js").write_text(scenario)
    (tmp_path / "harness.js").write_text(_HARNESS)
    proc = subprocess.run([NODE, str(tmp_path / "harness.js"), str(tmp_path / "main.js"),
                           str(tmp_path / "scenario.js")], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    got = json.loads(proc.stdout)
    assert got["errors"] == [], got["errors"]
    return got["out"]


def _nano(i: int, **over) -> dict:
    """An Exploris 480 nanoLC run: 44 min stored run length, 38 SPD."""
    base = dict(instrument_family="Exploris", instrument_model="Orbitrap Exploris 480", spd=38,
                lc_system="custom", gradient_length_min=44, cohort_id="Exploris_30spd_low", n_precursors=22000 + i)
    base.update(over)
    return _row(i, **base)


def _mixed_rows() -> list[dict]:
    """Two models, two Evosep gradients, a nanoLC gradient, two amounts, DIA and DDA, two columns."""
    rows = [_row(i) for i in range(6)]                                                        # HT, Evosep 100, 50 ng
    rows += [_row(100 + i, spd=60, gradient_length_min=21, cohort_id="timsTOF_60spd_low",
                  n_precursors=45000 + i) for i in range(6)]                                  # HT, Evosep 60, 50 ng
    rows += [_row(200 + i, amount_ng=200, n_precursors=52000 + i) for i in range(5)]          # HT, Evosep 100, 200 ng
    rows += [_row(300 + i, column_vendor="PepSep", column_model="PepSep MAX 10cm") for i in range(5)]
    rows += [_row(400 + i, column_vendor="IonOpticks", column_model="Aurora 25cm") for i in range(5)]
    rows += [_nano(500 + i) for i in range(6)]                                                # Exploris nanoLC
    rows += [_dda(600 + i) for i in range(5)]                                                 # HT DDA
    return rows


# ── server and page text ─────────────────────────────────────────────

def test_space_version_is_1_4_0(client, relay):
    assert relay.SPACE_VERSION == "1.4.0"
    assert client.get("/api/version").json()["version"] == "1.4.0"
    assert "community site v1.4.0" in _page(client)


def test_sticky_bar_markup_and_css(client):
    """§A.1 item 3: one bar under the stats row, above the reference ranges,
    sticky at the top of the screen, holding every field of the view."""
    html = _page(client)
    body = html[html.index("<body>"):]
    bar = body[body.index('<div class="fbar" id="fbar"'):body.index('<div class="section" id="where">')]
    assert body.index('id="stats"') < body.index('id="fbar"') < body.index('id="where"')
    assert 'role="region" aria-label="Filters for the benchmark panels"' in bar
    for control in ('id="sample-type-select" onchange="changeSampleType(this)"', 'id="fbar-model" onchange="setView({ model: this.value })"',
                    'id="fbar-gradient" onchange="setView({ gradient: this.value })"',
                    'id="fbar-amount" onchange="setView({ amount: this.value })"',
                    'id="fbar-column" onchange="setView({ column: this.value })"', 'id="fbar-inview"', 'id="fbar-reset"'):
        assert control in bar, control
    assert [m for m in re.findall(r'data-mode="(\w+)" aria-pressed', bar)] == ["dia", "dda", "all"]
    # The phone fold: a summary and a Filters button that owns the controls.
    assert 'id="fbar-summary" aria-live="polite"' in bar
    assert 'id="fbar-toggle" aria-expanded="false" aria-controls="fbar-ctrls" onclick="toggleFilterBar()"' in bar
    css = html[:html.index("</style>")]
    assert re.search(r"\.fbar \{ position: sticky; top: 0; z-index: 1500;", css)
    # Above Plotly's modebar (z-index 1001), below a full-screen chart (9999).
    assert 1001 < 1500 < 9999 and ".chart-card.fs { position: fixed; inset: 0;" in css
    # Anchors land below the bar: the root's scroll padding is the bar's height.
    assert "html { scroll-padding-top: var(--fbar-h); }" in css
    phone = css[css.index("@media (max-width: 640px) {\n            :root { --fbar-h: 3.2rem; }"):]
    phone = phone[:phone.index("\n        }\n")]
    assert ".fbar-sum { display: flex;" in phone and ".fbar-ctrls { display: none; }" in phone
    assert ".fbar.open .fbar-ctrls { display: grid;" in phone
    # The old sample-type row and its count badge are gone into the bar.
    assert 'id="sample-type-counts"' not in html and "QC Standard:</label>" not in html


def test_per_chart_amount_selects_and_tabs_are_views_of_the_state(client):
    html = _page(client)
    for sel in ("config-amount-filter", "violin-amount-filter", "spd-amount-filter"):
        m = re.search(rf'<select id="{sel}" data-amt onchange="setView\(\{{ amount: this.value \}}\)"', html)
        assert m, sel
    assert 'id="lab-amount"' not in html and 'id="lab-instrument"' not in html   # the trend follows the bar
    tabs = html[html.index('<div class="tabs"'):html.index("</div>", html.index('<div class="tabs"'))]
    assert re.findall(r'data-mode="(\w+)" onclick="showTab\(\'(\w+)\'\)"', tabs) == [("dia", "dia"), ("dda", "dda"), ("all", "all")]
    # The per-panel family and mode checkboxes above the cards are gone.
    assert 'id="ref-filters"' not in html and "toggleRefFilter" not in html
    main = _main_script(html)
    assert "function showTab(tab) { setView({ mode: tab }); }" in main


def test_every_panel_has_a_badge_that_says_what_it_follows(client):
    html = _page(client)
    for badge in ("ref-badge", "config-leaderboard-badge", "amount-mode-badge", "violin-mode-badge", "spd-depth-badge",
                  "column-compare-badge", "points-peak-badge", "mass-acc-badge", "ms1-signal-badge", "dyn-range-badge",
                  "pts-peak-badge", "lab-trend-badge", "table-badge"):
        assert re.search(rf'<span class="fbadge" id="{badge}"></span>', html), badge


# Pinned from main 54c4671 (relay 1.3.0): the TIC overlay (its code and its
# card, with its own SPD, LC and mode menus) and PEG Watch (section, CSS and
# script) are not touched by P2b. Recompute with the regions below over
# `git show 54c4671:hf_space/app.py` if a later phase changes them on purpose.
UNCHANGED = {
    "tic_js": "10b24afeeb91fb58ebf08f31aae48e474b6edacfd0a9e099ffa9214d32562275",
    "tic_card": "4d54f0b83481e79e18cffd73d6ff12d8d7ef53d50c012c7112059170f5ba7185",
    "peg_html": "a75df63c674d3b539fa04089dd9c279331e4f571d5837f6877df5061ae41f863",
    "peg_css": "e5cbfc43a53de637d392abcb200e763adcddf8da69bcddaef6780381fe4c68fc",
    "peg_js": "6678022131ca594e7ba08132e51418574557caf332bfa20037638dfb98a33fc0",
    "esc_js": "77f1dd1628d078cd667a60434507c8287c67ccd47a842657ffcf060cf5fec796",
}


def _regions(html: str) -> dict[str, str]:
    def between(a: str, b: str, incl_b: bool = False) -> str:
        i = html.index(a)
        j = html.index(b, i + len(a))
        return html[i:j + (len(b) if incl_b else 0)]
    return {
        "tic_js": between("// ── Community TIC Overlay", "\n// ── "),
        "tic_card": between("<h3>Community TIC Overlay by SPD</h3>", '<div id="chart-community-tic"></div>', True),
        "peg_html": between("<!-- Evosep PEG Watch (v1.2.0).", "<!-- Community Submissions -->"),
        "peg_css": between("/* ── Evosep PEG Watch (v1.2.0) ──", "#peg .peg-chips { justify-content: flex-start; }\n        }\n", True),
        "peg_js": re.search(r'<script id="peg-watch-js">.*?</script>', html, re.S).group(0),
        "esc_js": re.search(r'<script id="stan-esc">.*?</script>', html, re.S).group(0),
    }


def test_tic_overlay_and_peg_watch_are_byte_identical_to_54c4671(client):
    got = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in _regions(_page(client)).items()}
    assert got == UNCHANGED
    # The TIC still has its own menus, not the bar's.
    card = _regions(_page(client))["tic_card"]
    for sel in ('id="tic-spd-select"', 'id="tic-lc-select"', 'id="tic-mode-select"', 'id="tic-show-all"'):
        assert sel in card


def test_no_browser_storage(client):
    """The spec asks for no persistence, so the page keeps none: no storage
    call anywhere in its scripts (the harness below also makes any access throw)."""
    html = _page(client)
    scripts = "".join(re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S))
    assert "localStorage" not in scripts and "sessionStorage" not in scripts and "indexedDB" not in scripts
    assert "It is\n// not persisted (the spec does not ask for it)" in scripts


# ── page behaviour, in node ──────────────────────────────────────────

@needs_node
def test_page_loads_and_renders_with_storage_blocked(client, tmp_path):
    """Browser storage throws in the harness; the whole load path still runs."""
    rows = _mixed_rows()
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)});
        renderFilterBar(); updateStats(); renderTable(); renderRefRanges(); renderCharts();
        return [els['fbar-inview'].innerHTML, plots.length > 5];
    }})()"""
    inview, drew = _run(client, tmp_path, scenario)
    assert inview == "<b>28</b> runs in view · 1 lab" and drew


@needs_node
def test_b2_cohort_key(client, tmp_path):
    """model × mode × gradient × amount; Evosep named only by real methods,
    nanoLC by stored run length and derived SPD, missing LC never inferred."""
    scenario = """(() => {
        const r = (o) => Object.assign({ sample_type: 'hela', instrument_model: 'timsTOF HT', acquisition_mode: 'dia', amount_ng: 50 }, o);
        const cases = [
            r({ spd: 60, lc_system: 'evosep', gradient_length_min: 21 }),
            r({ spd: 40, lc_system: 'evosep', gradient_length_min: 31 }),
            r({ spd: 46, lc_system: 'evosep', gradient_length_min: 30 }),
            r({ spd: 38, lc_system: 'custom', gradient_length_min: 44 }),
            r({ spd: 38, lc_system: '', gradient_length_min: 44 }),
            r({ spd: 60, lc_system: '', gradient_length_min: 22 }),
            r({ spd: 0, lc_system: 'evosep' }),
            r({ spd: 38, lc_system: 'custom' }),
        ];
        const keys = cases.map(s => { const k = rowKey(s); return [k.lc, k.g, gradLabel(k.lc, k.spd, [s]), gradShort(k.lc, k.spd, [s])]; });
        const amounts = [0, null, 5, 25, 26, 50, 75, 76, 250, 251, 1000].map(a => amountBucketOf({ amount_ng: a }));
        const lens = runLenText([43, 44, 44, 44, 44, 44, 44, 44, 44, 44, 44, 96].map(g => ({ gradient_length_min: g })));
        return [keys, amounts, lens, rowKey(cases[0]).key, rowKey(cases[0]) === rowKey(cases[0])];
    })()"""
    keys, amounts, lens, key, cached = _run(client, tmp_path, scenario)
    assert keys == [
        ["evosep", "evosep:60", "Evosep 60 SPD", "Evosep 60 SPD"],
        ["evosep", "evosep:40", "Evosep Whisper 40 SPD", "Evosep Whisper 40 SPD"],
        ["evosep_unv", "evosep_unv:46", "Evosep, 30 min run (SPD 46 unverified)", "Evosep 46 SPD (unverified)"],
        # nanoLC leads with the gradient its SPD implies, 1440 / (1.25 × 38) = 30 min, then the stored run length
        ["nanolc", "nanolc:38", "~30 min gradient (38 SPD) · 44 min run", "~30 min gradient · 38 SPD · 44 min run"],
        ["nanolc", "nanolc:38", "~30 min gradient (38 SPD) · 44 min run", "~30 min gradient · 38 SPD · 44 min run"],  # no LC, not an Evosep SPD
        ["unrec", "unrec:60", "60 SPD, LC not recorded (22 min run)", "60 SPD, LC not recorded"],
        ["nospd", "nospd:0", "SPD not recorded", "SPD not recorded"],
        ["nanolc", "nanolc:38", "~30 min gradient (38 SPD)", "~30 min gradient · 38 SPD"],
    ]
    assert amounts == ["unk", "unk", "le25", "le25", "50", "50", "50", "100_250", "100_250", "gt250", "gt250"]
    assert lens == "44 min run"          # the 10th-90th percentile of the stored lengths
    assert key == "hela|timsTOF HT|DIA|evosep:60|50" and cached


@needs_node
def test_filter_state_drives_each_panel(client, tmp_path):
    rows = _mixed_rows()
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar();
        const last = (id) => plots.filter(p => p.id === id).slice(-1)[0];
        const snap = () => {{ renderPanels(null); return {{
            inview: els['fbar-inview'].innerHTML,
            groups: [...els['ref-ranges-container'].innerHTML.matchAll(/<h3><span class="mdot"[^>]*><\\/span>([^<]+)<\\/h3>/g)].map(m => m[1]),
            cards: (els['ref-ranges-container'].innerHTML.match(/<article class="rc">/g) || []).length,
            best: [...els['config-leaderboard'].innerHTML.matchAll(/<tr style="background[^>]*>(.*?)<\\/tr>/g)].length,
            violins: (last('chart-violin') || {{ traces: [] }}).traces.filter(t => t.type === 'violin').length,
            spd: ((last('chart-spd-depth') || {{ layout: {{}} }}).layout.annotations || []).map(a => a.text.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim()),
            amountX: [...new Set([].concat(...(last('chart-amount-depth') || {{ traces: [] }}).traces.map(t => t.x)))].sort(),
            cols: (last('chart-column-compare') || {{ traces: [] }}).traces.map(t => t.name).sort(),
            mh: (last('chart-points-peak') || {{ traces: [] }}).traces.filter(t => t.mode === 'markers' && t.showlegend === false).reduce((a, t) => a + t.x.length, 0),
            ms1: (last('chart-ms1-signal') || {{ traces: [] }}).traces.filter(t => t.mode === 'markers').map(t => [t.name, t.x.length]),
            table: (els['table-container'].innerHTML.match(/<span>(\\d+) submissions<\\/span>/) || [])[1],
            badges: Object.fromEntries(['ref-badge', 'config-leaderboard-badge', 'amount-mode-badge', 'violin-mode-badge', 'spd-depth-badge',
                'column-compare-badge', 'points-peak-badge', 'ms1-signal-badge', 'lab-trend-badge', 'table-badge'].map(b => [b, els[b].textContent])),
            mirrors: ['config-amount-filter', 'violin-amount-filter', 'spd-amount-filter'].map(i => els[i].value),
        }}; }};
        const out = {{ start: snap() }};
        setView({{ amount: 'all' }}); out.amountAll = snap();
        setView({{ amount: '50', model: 'timsTOF HT' }}); out.ht = snap();
        setView({{ gradient: 'evosep:60' }}); out.ht60 = snap();
        setView({{ gradient: '', column: 'pepsep max 10cm' }}); out.pepsep = snap();
        setView({{ column: '', model: '', mode: 'all' }}); out.both = snap();
        return out;
    }})()"""
    got = _run(client, tmp_path, scenario)
    s = got["start"]
    # default: HeLa, DIA, 50 ng: 6 + 6 + 5 + 5 HT Evosep 100/60 runs and 6 Exploris runs
    assert s["inview"] == "<b>28</b> runs in view · 1 lab" and s["table"] == "28"
    assert s["groups"] == ["timsTOF HT", "Orbitrap Exploris 480"]
    assert s["badges"]["ref-badge"] == "HeLa · DIA · 50 ng"
    assert s["badges"]["amount-mode-badge"] == "HeLa · DIA · all amounts"       # ignores the amount, and says so
    assert s["amountX"] == ["100–249 ng", "50 ng"] and s["mirrors"] == ["50", "50", "50"]
    assert s["best"] == 3 and s["violins"] == 3                                  # HT Evosep 100, HT Evosep 60, Exploris
    # amount: every amount; the 200 ng cohort is its own violin and table row
    a = got["amountAll"]
    assert a["inview"] == "<b>33</b> runs in view · 1 lab" and a["best"] == 4 and a["violins"] == 4
    assert a["mirrors"] == ["all", "all", "all"] and a["badges"]["violin-mode-badge"] == "HeLa · DIA · all amounts"
    # instrument
    h = got["ht"]
    assert h["groups"] == ["timsTOF HT"] and h["best"] == 2 and h["violins"] == 2
    assert [n for n, _ in h["ms1"]] == ["timsTOF HT runs"] and h["badges"]["ms1-signal-badge"] == "HeLa · DIA · timsTOF HT · 50 ng"
    assert h["spd"] == ["timsTOF HT 22 runs · 1 lab, 50 ng"]
    # gradient: panels that compare throughputs ignore it and say so
    g = got["ht60"]
    assert g["inview"] == "<b>6</b> runs in view · 1 lab" and g["best"] == 1 and g["violins"] == 1 and g["table"] == "6"
    assert g["spd"] == ["timsTOF HT 22 runs · 1 lab, 50 ng"]
    assert g["badges"]["spd-depth-badge"] == "HeLa · DIA · timsTOF HT · all gradients · 50 ng"
    assert g["badges"]["points-peak-badge"] == "HeLa · DIA · timsTOF HT · all gradients · 50 ng" and g["mh"] == 22
    assert g["badges"]["ref-badge"] == "HeLa · DIA · timsTOF HT · Evosep 60 SPD · 50 ng"
    # column: Column Comparison still compares every column
    c = got["pepsep"]
    assert c["inview"] == "<b>5</b> runs in view · 1 lab" and c["cols"] == ["IonOpticks Aurora 25cm", "PepSep MAX 10cm"]
    assert c["badges"]["column-compare-badge"] == "HeLa · DIA · timsTOF HT · 50 ng · all columns"
    assert c["badges"]["table-badge"] == "HeLa · DIA · timsTOF HT · 50 ng · PepSep MAX 10cm"
    # both modes: DIA and DDA each keep their own table and violins
    b = got["both"]
    assert b["inview"] == "<b>33</b> runs in view · 1 lab" and b["violins"] == 4
    assert b["badges"]["config-leaderboard-badge"] == "HeLa · DIA and DDA · 50 ng · sorted by DIA precursors, DDA psms"
    assert b["badges"]["amount-mode-badge"].endswith("⚠ precursors and PSMs on one axis")


@needs_node
def test_a_change_re_renders_only_the_panels_that_follow_it(client, tmp_path):
    rows = _mixed_rows()
    names = ["updateStats", "renderRefRanges", "renderConfigLeaderboard", "renderAmountDepth", "renderViolin",
             "renderSpdDepth", "renderColumnComparison", "renderPointsAcrossPeak", "renderCommunityTIC",
             "renderMassAccuracy", "renderMs1Signal", "renderDynamicRange", "renderPtsPerPeak", "renderLabTrend", "renderTable"]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar();
        let calls = [];
        for (const n of {json.dumps(names)}) {{ const f = globalThis[n]; globalThis[n] = function () {{ calls.push(n); return f.apply(this, arguments); }}; }}
        const out = {{}};
        for (const [label, patch] of [['amount', {{ amount: 'all' }}], ['gradient', {{ gradient: 'evosep:100' }}],
                                       ['column', {{ column: 'aurora 25cm' }}], ['model', {{ model: 'timsTOF HT' }}],
                                       ['mode', {{ mode: 'all' }}], ['sample', {{ sample: 'yeast' }}], ['same', {{ sample: 'yeast' }}]]) {{
            calls = []; const changed = setView(patch); out[label] = [[...changed].sort(), calls.slice().sort()];
        }}
        return out;
    }})()"""
    got = _run(client, tmp_path, scenario)
    every = sorted(set(names) - {"updateStats", "renderCommunityTIC"})
    minus = lambda *x: sorted(set(every) - set(x))  # noqa: E731
    assert got["amount"] == [["amount"], minus("renderAmountDepth")]
    assert got["gradient"] == [["gradient"], minus("renderSpdDepth", "renderPointsAcrossPeak")]
    assert got["column"] == [["column"], minus("renderColumnComparison")]
    assert got["model"] == [["model"], every]
    assert got["mode"] == [["mode"], every]
    # a new QC standard: every panel, the stats row and the TIC overlay; the
    # instrument, gradient and column picked for HeLa have no yeast runs
    assert got["sample"] == [["column", "gradient", "model", "sample"], sorted(names)]
    assert got["same"] == [[], []]


@needs_node
def test_facets_cascade_and_reset_what_they_rule_out(client, tmp_path):
    rows = _mixed_rows()
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar();
        const opts = (id) => els[id].options.map(o => o.value);
        const out = {{}};
        setView({{ gradient: 'evosep:60' }});
        out.modelsUnderGradient = opts('fbar-model');           // instruments follow sample, mode and amount only
        setView({{ model: 'Orbitrap Exploris 480' }});           // the Exploris runs no Evosep 60: the gradient resets
        out.afterModel = {{ ...view }};
        out.gradients = opts('fbar-gradient');
        setView({{ gradient: 'nanolc:38', column: '' }});
        out.columns = opts('fbar-column');                      // Exploris records no column
        setView({{ model: 'timsTOF HT', column: 'aurora 25cm' }});
        out.picked = {{ ...view }};
        setView({{ mode: 'dda' }});                              // no DDA run on Aurora: the column resets
        out.dda = {{ ...view }};
        setView({{ model: '<nope>' }});                          // a value that is not offered is dropped
        out.nope = view.model;
        resetView(); out.reset = {{ ...view }}; out.resetHidden = els['fbar-reset'].hidden;
        return out;
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert got["modelsUnderGradient"] == ["", "timsTOF HT", "Orbitrap Exploris 480"]
    assert got["afterModel"]["model"] == "Orbitrap Exploris 480" and got["afterModel"]["gradient"] == ""
    assert got["gradients"] == ["", "nanolc:38"]
    assert got["columns"] == [""]
    assert got["picked"]["column"] == "aurora 25cm" and got["picked"]["model"] == "timsTOF HT"
    assert got["dda"]["column"] == "" and got["dda"]["model"] == "timsTOF HT" and got["dda"]["mode"] == "dda"
    assert got["nope"] == ""
    assert got["reset"] == {"sample": "hela", "mode": "dia", "model": "", "gradient": "", "amount": "50", "column": ""}
    assert got["resetHidden"] is True


@needs_node
def test_bar_counts_tabs_and_phone_summary(client, tmp_path):
    """The bar shows the runs in view; at 400 px it folds to a one-line summary
    (CSS), which says the whole view, and the Filters button opens it."""
    rows = _mixed_rows() + [_row(900 + i, sample_type="yeast") for i in range(2)]
    scenario = f"""(() => {{
        const mk = (mode, tab) => {{ const e = new El(''); e.setAttribute('data-mode', mode); if (tab) e.classList.add('tab'); return e; }};
        const bar = [mk('dia'), mk('dda'), mk('all')], tabs = [mk('dia', 1), mk('dda', 1), mk('all', 1)];
        qsa['#fbar-mode button[data-mode], .tabs .tab[data-mode]'] = bar.concat(tabs);
        setSubmissions({json.dumps(rows)});
        document.getElementById('fbar').offsetHeight = 38;
        renderFilterBar();
        const start = {{ summary: els['fbar-summary'].innerHTML, inview: els['fbar-inview'].innerHTML,
                         samples: els['sample-type-select'].innerHTML, n: [els['fbar-n-dia'].textContent, els['fbar-n-dda'].textContent, els['fbar-n-all'].textContent],
                         pressed: bar.map(b => b.getAttribute('aria-pressed')), active: tabs.map(t => t.classList.contains('active')),
                         padding: root.style.props['--fbar-h'] }};
        showTab('dda');
        const dda = {{ pressed: bar.map(b => b.getAttribute('aria-pressed')), active: tabs.map(t => t.classList.contains('active')), mode: view.mode }};
        setView({{ mode: 'dia', model: 'timsTOF HT', gradient: 'evosep:60' }});
        const narrowed = els['fbar-summary'].innerHTML;
        toggleFilterBar();
        const open = [els['fbar'].classList.contains('open'), els['fbar-toggle'].getAttribute('aria-expanded'), els['fbar-toggle'].textContent];
        toggleFilterBar();
        const shut = [els['fbar'].classList.contains('open'), els['fbar-toggle'].getAttribute('aria-expanded'), els['fbar-toggle'].textContent];
        return {{ start, dda, narrowed, open, shut }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    s = got["start"]
    assert s["summary"] == "<b>HeLa · DIA · 50 ng</b> · 28 runs"
    assert s["inview"] == "<b>28</b> runs in view · 1 lab"
    assert "HeLa · 28</option>" in s["samples"] and "Yeast · 2</option>" in s["samples"] and "All standards · 30</option>" in s["samples"]
    assert s["n"] == ["28", "5", "33"]
    assert s["pressed"] == ["true", "false", "false"] and s["active"] == [True, False, False]
    assert s["padding"] == "38px"                       # the scroll padding is the bar's folded height
    assert got["dda"] == {"pressed": ["false", "true", "false"], "active": [False, True, False], "mode": "dda"}
    assert got["narrowed"] == "<b>HeLa · DIA · 50 ng</b> · timsTOF HT · Evosep 60 SPD · 6 runs"
    assert got["open"] == [True, "true", "Done"] and got["shut"] == [False, "false", "Filters"]


@needs_node
def test_hostile_names_are_escaped_in_the_bar_and_badges(client, tmp_path):
    rows = [_row(i, instrument_model=EVIL, instrument_family=EVIL, column_vendor="<b>v</b>", column_model=EVIL,
                 display_name=EVIL) for i in range(6)]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar();
        setView({{ model: {json.dumps(EVIL)} }});
        setView({{ column: {json.dumps(EVIL.lower())} }});
        return [els['fbar-model'].innerHTML, els['fbar-column'].innerHTML, els['fbar-summary'].innerHTML,
                els['ref-badge'].textContent, view.model, view.column, els['ref-ranges-container'].innerHTML];
    }})()"""
    model_opts, col_opts, summary, badge, model, column, cards = _run(client, tmp_path, scenario)
    for html in (model_opts, col_opts, summary, cards):
        assert "<img" not in html and "<b>v</b>" not in html and "&lt;img" in html
    assert model == EVIL and column == EVIL.lower()      # the value round-trips through the escaped option
    assert badge.startswith("HeLa · DIA · " + EVIL)     # a badge is text, never HTML


def _trend_rows() -> list[dict]:
    """Lab A: 32 runs in HT Evosep 60 (the cohort): 30 that fix its baseline, then
    one collapse and one normal run. 6 more at 200 ng (another cohort), 5 in HT
    Evosep 100. Lab B: 7 runs in the cohort. Anonymous Lab: 6."""
    def run(i, lab, day, prec, **over):
        over.setdefault("spd", 60)
        over.setdefault("gradient_length_min", 21)
        return _row(i, display_name=lab, n_precursors=prec,
                    run_date=f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}T10:00:00Z", **over)
    rows = [run(i, "Lab A", i, 40000 + (i % 5) * 100) for i in range(30)]
    rows += [run(30, "Lab A", 150, 12000), run(31, "Lab A", 160, 40100)]                 # one collapse, one normal
    rows += [run(100 + i, "Lab A", 30 + i, 60000, amount_ng=200) for i in range(6)]      # same lab, 200 ng: not this cohort
    rows += [run(200 + i, "Lab A", 40 + i, 35000, spd=100, gradient_length_min=11) for i in range(5)]
    rows += [run(300 + i, "Lab B", 10 + i, 30000 + i * 1000) for i in range(7)]
    rows += [run(400 + i, "Anonymous Lab", 20 + i, 90000) for i in range(6)]
    return rows


@needs_node
def test_lab_trend_matches_the_cohort_and_excludes_the_lab(client, tmp_path):
    """B3: the reference is the same cohort without the selected lab (and
    without "Anonymous Lab", which may be the lab itself), as percentile bands;
    the baseline is the lab's own first runs; the panel opens on the lab with
    the most recent runs."""
    scenario = f"""(() => {{
        setSubmissions({json.dumps(_trend_rows())}); renderFilterBar();
        const trace = () => plots.filter(p => p.id === 'chart-lab-trend').slice(-1)[0];
        const state = () => {{ const p = trace(); return {{
            lab: els['lab-select'].value, labs: els['lab-select'].innerHTML, cohort: els['lab-cohort'].value, cohorts: els['lab-cohort'].innerHTML,
            names: p.traces.map(t => t.name), mine: p.traces.find(t => /'s runs$/.test(t.name)),
            bands: p.traces.filter(t => /^Other labs/.test(t.name)).map(t => [Math.min(...t.y), Math.max(...t.y)]),
            out: (p.traces.find(t => t.name === 'Outside the baseline band') || {{ x: [] }}).x.length,
            ytitle: p.layout.yaxis.title, note: els['lab-trend-note'].innerHTML, sum: els['lab-trend-sum'].innerHTML }}; }};
        renderLabTrend();
        const a = state();
        pickTrend('lab', 'Lab B'); const b = state();
        pickTrend('lab', 'Anonymous Lab'); const anon = state();
        pickTrend('lab', 'Lab A'); pickTrend('cohort', 'hela|timsTOF HT|DIA|evosep:100|50'); const a100 = state();
        pickTrend('metric', 'peptides'); const pep = state();
        setView({{ gradient: 'evosep:60' }}); pickTrend('metric', 'primary'); const filtered = state();
        return {{ a, b, anon, a100, pep, filtered }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    a = got["a"]
    # Lab A has the most recent run (day 160), so it opens there, in its busiest cohort
    assert a["lab"] == "Lab A" and a["cohort"] == "hela|timsTOF HT|DIA|evosep:60|50"
    assert "timsTOF HT · DIA · Evosep 60 SPD · 50 ng (32 runs)" in a["cohorts"]
    assert "timsTOF HT · DIA · Evosep 100 SPD · 50 ng (5 runs)" in a["cohorts"]
    assert "200 ng" not in a["cohorts"] and "100–250 ng" not in a["cohorts"]   # the 200 ng runs are the bar's other amount
    assert len(a["mine"]["x"]) == 32                   # its own runs in this cohort only
    # reference: Lab B's 7 runs, never Lab A's own or Anonymous Lab's 90,000s
    assert a["bands"] and max(hi for _, hi in a["bands"]) <= 36000 and min(lo for lo, _ in a["bands"]) >= 30000
    assert "Other labs, 10–90th pct (7 runs · 1 lab)" in a["names"]
    assert "Baseline ± 3 robust SD (1.4826 × MAD)" in a["names"] and "Median of the last 15 runs" in a["names"]
    assert a["out"] == 1 and a["ytitle"] == "Precursors (1% FDR)"
    assert a["note"] == ""
    assert "Baseline <b>40,200</b>" in a["sum"] and "<b>1</b> of 2 later runs shown fell outside it" in a["sum"]
    assert "the cohort holds 45 runs · 2 labs" in a["sum"]
    # Lab B: its reference is Lab A (32 runs), and it has no baseline yet
    b = got["b"]
    assert "Other labs, 10–90th pct (32 runs · 1 lab)" in b["names"] and not any("aseline" in n for n in b["names"])
    assert "7 of the 20 needed" in b["note"]
    # Anonymous Lab: the named labs are its reference
    assert "Other labs, 10–90th pct (39 runs · 2 labs)" in got["anon"]["names"]
    # a cohort with no other lab says so
    a100 = got["a100"]
    assert not a100["bands"] and "No other lab in this cohort yet." in a100["note"]
    assert got["pep"]["ytitle"] == "Peptides (1% FDR)"
    # the lab and cohort lists follow the filter bar
    f = got["filtered"]
    assert f["cohorts"].count("<option") == 1 and "Evosep 100 SPD" not in f["cohorts"]


@needs_node
def test_lab_trend_escapes_lab_names(client, tmp_path):
    rows = [_row(i, display_name=EVIL, run_date=f"2026-03-{1 + i:02d}T10:00:00Z") for i in range(6)]
    rows += [_row(50 + i, display_name="Lab <b>B</b>", run_date=f"2026-02-{1 + i:02d}T10:00:00Z") for i in range(6)]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderLabTrend();
        const p = plots.filter(p => p.id === 'chart-lab-trend').slice(-1)[0];
        return [els['lab-select'].innerHTML, els['lab-trend-note'].innerHTML, els['lab-trend-sum'].innerHTML,
                JSON.stringify(p.traces.map(t => [t.name, t.text || null])), els['lab-select'].value];
    }})()"""
    labs, note, summary, traces, lab = _run(client, tmp_path, scenario)
    for html in (labs, note, summary, traces):
        assert "<img" not in html and "<b>B</b>" not in html
    assert "&lt;img" in labs and "Lab &lt;b&gt;B&lt;/b&gt;" in labs
    assert "&lt;img" in summary and "&lt;img" in traces
    assert lab == EVIL


@needs_node
def test_lab_trend_file_names_never_appear(client, tmp_path):
    from tests.test_relay_community_p1 import SECRET_NAME
    scenario = f"""(() => {{ setSubmissions({json.dumps(_trend_rows())}); renderLabTrend();
        return JSON.stringify(plots) + els['lab-select'].innerHTML + els['lab-cohort'].innerHTML + els['lab-trend-sum'].innerHTML; }})()"""
    assert SECRET_NAME not in _run(client, tmp_path, scenario)


@needs_node
def test_a_filter_change_over_3400_rows_is_quick(client, tmp_path):
    """About 3,350 rows are processed client-side: every filter change is a
    few linear passes, never a pass per row."""
    rows = []
    for i in range(3400):
        kind = i % 4
        if kind == 3:
            rows.append(_nano(i, spd=[38, 19, 12][i % 3], gradient_length_min=[44, 88, 96][i % 3]))
        else:
            rows.append(_row(i, spd=[100, 60, 30][kind], gradient_length_min=[11, 21, 44][kind],
                             run_date=f"20{20 + i % 7}-{1 + i % 12:02d}-{1 + i % 28:02d}T10:00:00Z",
                             display_name=["Lab A", "Lab B", "Anonymous Lab"][i % 3]))
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderPanels(null);
        const t0 = Date.now();
        for (const p of [{{ amount: 'all' }}, {{ model: 'timsTOF HT' }}, {{ gradient: 'evosep:60' }}, {{ mode: 'all' }},
                         {{ gradient: '' }}, {{ model: '' }}, {{ amount: '50' }}, {{ mode: 'dia' }}]) setView(p);
        return [Date.now() - t0, els['fbar-inview'].innerHTML];
    }})()"""
    ms, inview = _run(client, tmp_path, scenario)
    assert inview.startswith("<b>3,400</b> runs in view")
    assert ms < 4000, f"8 filter changes over 3,400 rows took {ms} ms"


# ── Review fixes before the P2b deploy (2026-09-30) ──────────────────

def _count_rows() -> list[dict]:
    """_mixed_rows() plus low-load, yeast and K562 runs, so picking an option
    can clear a gradient, an instrument or a column through the cascade."""
    rows = _mixed_rows()
    rows += [_row(700 + i, amount_ng=10, n_precursors=30000 + i) for i in range(2)]                 # HT Evosep 100, ≤25 ng
    rows += [_row(720 + i, sample_type="yeast") for i in range(3)]                                   # yeast, HT Evosep 100
    rows += [_nano(740 + i, sample_type="yeast") for i in range(2)]                                  # yeast, Exploris
    rows += [_row(760 + i, sample_type="k562", spd=60, gradient_length_min=21) for i in range(2)]    # K562, HT Evosep 60
    return rows


@needs_node
def test_every_option_count_is_the_view_it_leads_to(client, tmp_path):
    """Review finding 1: a count must be what the page shows after picking it,
    cascade included ("DDA · 0" once showed 36 after clearing the gradient)."""
    starts = [{}, {"gradient": "evosep:60"}, {"model": "timsTOF HT", "column": "pepsep max 10cm"},
              {"mode": "all", "amount": "all"}, {"sample": "yeast"}, {"model": "Orbitrap Exploris 480", "amount": "le25"},
              {"mode": "dda", "gradient": "evosep:100"}]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(_count_rows())}); renderFilterBar();
        const inView = () => +els['fbar-inview'].innerHTML.match(/<b>([\\d,]+)<\\/b>/)[1].replace(/,/g, '');
        const count = (label) => +label.split(' · ').pop().replace(/,/g, '');
        const restore = (v) => {{ Object.assign(view, v); applyFilters(); renderFilterBar(); }};
        const opts = (id) => [...els[id].innerHTML.matchAll(/<option value="([^"]*)"[^>]*>([^<]*)<\\/option>/g)].map(m => [m[1], m[2]]);
        const bad = [], seen = {{}};
        let checks = 0;
        for (const start of {json.dumps(starts)}) {{
            restore({{ ...VIEW_DEFAULT }}); setView(start); const base = {{ ...view }};
            seen[JSON.stringify(start)] = base;
            const controls = [['sample', 'sample-type-select'], ['model', 'fbar-model'], ['gradient', 'fbar-gradient'],
                              ['amount', 'fbar-amount'], ['column', 'fbar-column']];
            for (const [field, id] of controls) {{
                restore(base);
                for (const [value, label] of opts(id)) {{
                    restore(base); setView({{ [field]: value }}); checks++;
                    if (count(label) !== inView()) bad.push([JSON.stringify(start), field, value, label, inView(), {{ ...view }}]);
                }}
            }}
            for (const m of ['dia', 'dda', 'all']) {{
                restore(base); const n = +els['fbar-n-' + m].textContent.replace(/,/g, '');
                setView({{ mode: m }}); checks++;
                if (n !== inView()) bad.push([JSON.stringify(start), 'mode', m, n, inView(), {{ ...view }}]);
            }}
        }}
        return {{ bad, checks, seen }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert got["bad"] == [], got["bad"][:5]
    assert got["checks"] > 100
    # the starting views are what the bar resolves them to (a cleared facet shows here)
    assert got["seen"]['{"gradient":"evosep:60"}']["gradient"] == "evosep:60"
    assert got["seen"]['{"mode":"dda","gradient":"evosep:100"}']["gradient"] == "evosep:100"
    assert got["seen"]['{"model":"Orbitrap Exploris 480","amount":"le25"}']["model"] == ""   # no Exploris run at ≤25 ng


@needs_node
def test_option_counts_follow_the_cascade_in_the_cases_the_review_found(client, tmp_path):
    scenario = f"""(() => {{
        setSubmissions({json.dumps(_count_rows())}); setView({{ gradient: 'evosep:60' }});
        const label = (id, v) => [...els[id].innerHTML.matchAll(/<option value="([^"]*)"[^>]*>([^<]*)<\\/option>/g)].find(m => m[1] === v)[2];
        return [els['fbar-n-dda'].textContent, label('fbar-amount', 'le25'), label('fbar-model', 'timsTOF HT'),
                label('fbar-model', ''), label('fbar-gradient', '')];
    }})()"""
    dda, le25, ht, all_models, all_grads = _run(client, tmp_path, scenario)
    assert dda == "5"                              # picking DDA clears Evosep 60 (DIA only) and shows the 5 DDA runs
    assert le25 == "≤25 ng · 2"                    # likewise the two 10 ng runs
    assert ht == "timsTOF HT · 6" and all_models == "All instruments · 6"   # under Evosep 60: its 6 runs
    assert all_grads == "All gradients · 28"


def test_page_text_after_review(client):
    html = _page(client)
    main = _main_script(html)
    # 2: the band is a robust SD, named so wherever it is described
    assert "&plusmn; 3 MAD" not in html and "± 3 MAD" not in main
    trend = html[html.index('<div class="section" id="trend">'):html.index("<!-- Evosep PEG Watch (v1.2.0).")]
    assert "median\n        &plusmn; 3 robust SD (1.4826 &times; MAD) of its first 30 runs" in trend
    assert "provisional until there are 30" in trend
    assert "`${pre} ± 3 robust SD (1.4826 × MAD)`" in main
    # 3: the Explorer intro names Depth by Amount as the one chart that pools DIA and DDA
    explore = html[html.index('<div class="section" id="explore">'):html.index("<!-- Best Configurations (B6)")]
    assert "with one\n        exception: Depth by Amount Loaded puts precursors and PSMs on one axis" in explore
    assert "DIA and DDA are never pooled" not in explore
    # 8: every reason a cohort is not ranked
    where = html[html.index('<div class="section" id="where">'):html.index('<div class="section" id="join">')]
    assert "fewer than 5 runs,\n        records no SPD, records no LC at an SPD that is also an Evosep method" in where


@needs_node
def test_lab_picker_names_anonymous_lab_and_counts_dated_runs(client, tmp_path):
    """4: "Anonymous Lab" is every unclaimed install. 5: an undated run is never
    plotted, so it never counts toward a lab's 5 runs."""
    rows = _trend_rows()
    rows += [_row(800 + i, display_name="Lab C", spd=60, gradient_length_min=21, run_date=f"2026-02-{1 + i:02d}T10:00:00Z")
             for i in range(4)]
    rows += [_row(820 + i, display_name="Lab C", spd=60, gradient_length_min=21, run_date=None, submitted_at=None)
             for i in range(3)]                                                                  # 4 dated + 3 undated
    rows += [_row(840 + i, display_name="Lab D", spd=60, gradient_length_min=21, run_date=f"2026-02-{1 + i:02d}T11:00:00Z")
             for i in range(5)]
    rows += [_row(860 + i, display_name="Lab D", spd=60, gradient_length_min=21, run_date=None, submitted_at=None)
             for i in range(2)]                                                                  # 5 dated + 2 undated
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderLabTrend();
        const labs = els['lab-select'].innerHTML;
        pickTrend('lab', 'Lab D');
        return [labs, els['lab-cohort'].innerHTML];
    }})()"""
    labs, cohorts = _run(client, tmp_path, scenario)
    assert '<option value="Anonymous Lab">Anonymous Lab (unclaimed; may be several labs), latest ' in labs
    assert ">Lab A (latest " in labs
    assert "Lab C" not in labs                  # 4 dated runs: not listed
    assert ">Lab D (latest " in labs and "Evosep 60 SPD · 50 ng (5 runs)" in cohorts


@needs_node
def test_a_baseline_is_provisional_until_30_runs(client, tmp_path):
    """6: from 20 to 29 runs the baseline is every run so far, so it moves and
    nothing is judged; the panel says provisional, not fixed."""
    rows = [_row(i, spd=60, gradient_length_min=21, run_date=f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}T10:00:00Z",
                 n_precursors=40000 + (i % 5) * 100) for i in range(25)]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderLabTrend();
        const p = plots.filter(p => p.id === 'chart-lab-trend').slice(-1)[0];
        return [p.traces.map(t => t.name), els['lab-trend-note'].innerHTML, els['lab-trend-sum'].innerHTML];
    }})()"""
    names, note, summary = _run(client, tmp_path, scenario)
    assert "Provisional baseline ± 3 robust SD (1.4826 × MAD)" in names
    assert "Provisional baseline: median of the lab's 25 runs so far" in names
    assert "Outside the baseline band" not in names
    assert "<b>Provisional baseline:</b> 25 of the 30 runs that fix it" in note and "nothing is flagged" in note
    assert "from Clogged PeakTail's own runs so far in this cohort" in note
    assert "Provisional baseline <b>40,200</b> (median ± 3 robust SD (1.4826 × MAD): " in summary
    assert "from the 30th run" in summary and "fixed from" not in summary


@needs_node
def test_nanolc_names_lead_with_the_gradient_everywhere(client, tmp_path):
    """9: "~30 min gradient (38 SPD) · 44 min run", the gradient 1440 / (1.25 × 38)
    implies first, on the cards, Best Configurations, the violins, the table
    and the lab-trend cohort picker; the cohort key is unchanged."""
    name = "~30 min gradient (38 SPD) · 44 min run"
    scenario = f"""(() => {{
        setSubmissions({json.dumps(_mixed_rows())}); renderFilterBar(); renderPanels(null);
        setView({{ model: 'Orbitrap Exploris 480' }});
        const violin = plots.filter(p => p.id === 'chart-violin').slice(-1)[0];
        const ticks = violin.layout.xaxis.ticktext;
        return [els['ref-ranges-container'].innerHTML, els['config-leaderboard'].innerHTML, ticks,
                els['table-container'].innerHTML, els['lab-cohort'].innerHTML, els['fbar-gradient'].innerHTML,
                rowKey(allData.find(s => s.instrument_model === 'Orbitrap Exploris 480')).g];
    }})()"""
    cards, best, ticks, table, picker, grads, key = _run(client, tmp_path, scenario)
    assert f"<h4>{name}</h4>" in cards
    assert f">{name}</td>" in best
    assert ticks == ["Exploris 480<br>~30 min gradient<br>38 SPD<br>44 min run"]
    assert f"{name} · 50 ng" in table
    assert f"Exploris 480 · DIA · {name} · 50 ng (6 runs)" in picker
    assert f"{name} · 6</option>" in grads
    assert key == "nanolc:38"
    assert "44 min run (~38 SPD)" not in cards + best + table + picker


@needs_node
def test_table_cohort_names_the_column_under_a_column_filter(client, tmp_path):
    """7: under a column filter the percentile is among that column's runs, so
    the cohort says so."""
    scenario = f"""(() => {{
        setSubmissions({json.dumps(_mixed_rows())}); renderFilterBar();
        renderTable(); const before = els['table-container'].innerHTML;
        setView({{ column: 'pepsep max 10cm' }});
        return [before, els['table-container'].innerHTML];
    }})()"""
    before, after = _run(client, tmp_path, scenario)
    assert "Evosep 100 SPD · 50 ng <span" in before and "PepSep" not in before.split("<tbody>")[1].split("Evosep 100 SPD · 50 ng")[1][:40]
    assert "Evosep 100 SPD · 50 ng · PepSep MAX 10cm <span class=\"nr-why\">(n=5)</span>" in after


@needs_node
def test_stats_tile_says_it_is_the_whole_standard(client, tmp_path):
    """10: the tile counts every mode and amount; the bar's count is the view."""
    rows = _mixed_rows()
    scenario = f"""(() => {{ setSubmissions({json.dumps(rows)}); updateStats(); renderFilterBar();
        return [els['stat-submissions'].textContent, els['stat-runs-sub'].textContent, els['fbar-inview'].innerHTML]; }})()"""
    tile, sub, inview = _run(client, tmp_path, scenario)
    assert tile == "38" and inview.startswith("<b>28</b> runs in view")
    assert sub == "HeLa runs, every mode and amount; the filter bar below narrows the view"
