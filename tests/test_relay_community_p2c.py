"""Community site redesign, phase P2c ("Where does my run sit?"), in the vendored relay.

Spec: docs/superpowers/specs/2026-09-29-community-redesign-and-precursor-lookup-design.md
(§A.3 B1, Part B §B.2/§B.4/§B.10, decision 11); research basis in
docs/community-redesign/precursor-lookup/; mockup in docs/community-redesign/mockup/.

Decision of 2026-10-01: ship the lookup for matching searches only.
DIA-NN 2.3.x against the frozen community library at 1% run-level FDR (DDA:
Sage 0.14.x against the frozen FASTA at 1% PSM FDR) is placed in its cohort;
every other search gets a specific refusal. No scaling factor, preliminary or
otherwise, is applied to any search.

Server side: SPACE_VERSION 1.5.0. Page side, as text: the lookup holds the
#where anchor with the reference ranges right below it, no network or storage
call in its code, TIC overlay, PEG Watch and the P2a/P2b read-time rules
byte-identical to main fc5cb33. Page side, run in node against the page's own
script (the harness of tests/test_relay_community_p2b.py, plus a FileReader,
and network APIs that record any call): the percentile against a hand
computation, the cohort key and ranking rules, defaults from the filter bar,
each refusal, real DIA-NN logs from Hive (tests/fixtures/diann_logs/), hostile
file contents, and no network call on submit.

The fixtures are real DIA-NN report.log.txt files of HeLa QC searches,
copied from Hive on 2026-10-01: whole for the two one-file STAN searches, the
first 60 lines for the others. Only identifying text was replaced (user and
project directories, host names, project raw-file prefixes, an output name);
the banners, options, library file names, echo lines, counts and line ends
(CRLF on Windows, see .gitattributes) are DIA-NN's own:
  stan_hive_2.3.0_timstof_report.log.txt         STAN production on Hive, timsTOF HT, subset library
  stan_pc_2.3.2_exploris_report.log.txt          STAN's Windows install on an Exploris 480, frozen Orbitrap library
  diann_2.7.0_predicted_4files_head.log.txt      DIA-NN 2.7.0, predicted library, 4 timsTOF files, no MBR
  diann_2.3.0_gui_predicted_mbr_12files_head.log.txt  DIA-NN 2.3.0 GUI, predicted library, 12 files, MBR
  diann_1.9_gui_libfree_mbr_2files_head.log.txt  DIA-NN 1.9 GUI, library-free (empty --lib), 2 Exploris files, MBR
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from tests.test_relay_community_p1 import _dda, _main_script, _row
from tests.test_relay_community_p2b import UNCHANGED, _regions
from tests.test_relay_peg import NODE, _page, client, hub, needs_node, relay  # noqa: F401  (fixtures)

EVIL = '<img src=x onerror="alert(1)">'
LOGS = Path(__file__).parent / "fixtures" / "diann_logs"

# ── harness ──────────────────────────────────────────────────────────

_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = fs.readFileSync(process.argv[3], 'utf8');
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
const qsa = {};
const errors = [];
const con = { log() {}, info() {}, warn() {}, error: (...a) => errors.push(a.map(x => (x && x.stack) || String(x)).join(' ')) };
const root = { style: { props: {}, setProperty(k, v) { this.props[k] = v; } } };
const blocked = () => { throw new Error('SecurityError: browser storage is blocked here'); };
// Every way a page can reach the network records the call here.
const net = [];
const rec = (what) => function (...a) { net.push(what + ' ' + String(a[0])); return what === 'fetch' ? new Promise(() => {}) : undefined; };
class XMLHttpRequest { open(m, u) { net.push('xhr ' + u); } send() { net.push('xhr-send'); } setRequestHeader() {} }
class WebSocket { constructor(u) { net.push('ws ' + u); } }
class EventSource { constructor(u) { net.push('sse ' + u); } }
class Image { set src(u) { net.push('img ' + u); } }
// A FileReader that hands back the slice it was given, so a test can see how
// much of a file the page asked for.
const reads = [];
class FileReader {
    readAsText(blob) { reads.push(blob.__range); this.result = blob.__text; if (this.onload) this.onload(); }
}
const win = { addEventListener() {}, innerWidth: 1280 };
['localStorage', 'sessionStorage'].forEach(k => Object.defineProperty(win, k, { get: blocked }));
const ctx = vm.createContext({
    console: con, els, qsa, root, El, net, reads,
    document: { getElementById: byId, querySelector: () => null, querySelectorAll: (s) => qsa[s] || [],
                addEventListener() {}, createElement: () => new El(''), body: new El('body'), documentElement: root },
    window: win, navigator: { sendBeacon: rec('beacon'), userAgent: 'node' },
    fetch: rec('fetch'), XMLHttpRequest, WebSocket, EventSource, Image, FileReader,
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    Plotly: { newPlot() {}, Plots: { resize() {} }, purge() {} },
});
['localStorage', 'sessionStorage'].forEach(k => Object.defineProperty(ctx, k, { get: blocked }));
vm.runInContext(src, ctx);
// A File as the browser hands one over: slice() gives a Blob of that range.
vm.runInContext(`function fakeFile(text) { return { size: text.length, name: 'report.log.txt',
    slice(a, b) { return { __range: [a, b], __text: text.slice(a, b) }; } }; }`, ctx);
const out = vm.runInContext(scenario, ctx);
process.stdout.write(JSON.stringify({ out, errors, net }));
process.exit(0);
"""


def _run(client, tmp_path: Path, scenario: str, allow_load_fetch: bool = True):
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
    # The page's own load asks /api/leaderboard once; nothing else may reach the network.
    assert got["net"] == (["fetch /api/leaderboard"] if allow_load_fetch else []), got["net"]
    return got["out"]


def _text(html: str) -> str:
    """What a reader sees: block tags break words, inline tags (b, code, a) do not."""
    h = re.sub(r"</?(?:div|li|ul|p|br|small|svg|option|optgroup|span)\b[^>]*>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h)).strip()


# The search fields of the community search, and a matching DIA lookup.
MATCH_DIA = "lkSet('eng', 'diann'); lkSet('ver', '2.3'); lkSet('lib', 'frozen'); lkSet('fdr', 'run1'); lkSet('runs', 'alone');"
MATCH_DDA = "lkSet('mode', 'DDA'); lkSet('eng', 'sage'); lkSet('ver', '0.14'); lkSet('lib', 'frozen'); lkSet('fdr', 'psm1');"


def _ht60(values: list[int], **over) -> list[dict]:
    """timsTOF HT, DIA, Evosep 60 SPD, 50 ng: one run per value."""
    return [_row(1000 + i, spd=60, gradient_length_min=21, n_precursors=v, n_peptides=20000 + i,
                 run_date=f"2026-0{1 + i % 9}-{1 + i % 28:02d}T{i % 24:02d}:00:00Z", **over)
            for i, v in enumerate(values)]


def _nano(i: int, **over) -> dict:
    """An Exploris 480 nanoLC run: 44 min stored run length, 38 SPD."""
    base = dict(instrument_family="Exploris", instrument_model="Orbitrap Exploris 480", spd=38, lc_system="custom",
                gradient_length_min=44, cohort_id="Exploris_30spd_low", n_precursors=22000 + 10 * i, n_peptides=18000 + i)
    base.update(over)
    return _row(i, **base)


def _mid_rank_pct(val: float, values: list[float]) -> float:
    """Independent reference: share of runs below, ties counting half (the spec's mid-rank)."""
    below = sum(1 for x in values if x < val)
    ties = sum(1 for x in values if x == val)
    return 100 * (below + ties / 2) / len(values)


def _interp_quantile(values: list[float], p: float) -> float:
    s = sorted(values)
    x = (len(s) - 1) * p
    lo = int(x)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (x - lo)


# ── server and page text ─────────────────────────────────────────────

def test_space_version_is_1_5_0(client, relay):
    assert relay.SPACE_VERSION == "1.5.0"
    assert client.get("/api/version").json()["version"] == "1.5.0"
    assert "community site v1.5.0" in _page(client)


def test_where_anchor_is_the_lookup_and_the_ranges_follow(client):
    html = _page(client)
    body = html[html.index("<body>"):]
    nav = body[body.index('<nav class="nav"'):body.index("</nav>")]
    assert '<a href="#where">Where do I stand</a>' in nav
    assert body.count('id="where"') == 1 and body.count('id="ranges"') == 1
    where = body[body.index('<div class="section" id="where">'):body.index('<div class="section" id="ranges">')]
    assert "<h2>Where does my run sit?</h2>" in where
    # under the filter bar, with the reference ranges right below it
    assert body.index('id="fbar"') < body.index('id="where"') < body.index('id="ranges"') < body.index('id="join"')
    ranges = body[body.index('<div class="section" id="ranges">'):body.index('<div class="section" id="join">')]
    assert "<h2>Reference ranges</h2>" in ranges and 'id="ref-ranges-container"' in ranges
    # every field the brief lists, and the optional log drop
    for field in ('id="ws-val"', 'id="ws-eng"', 'id="ws-ver"', 'id="ws-lib"', 'id="ws-fdr"', 'id="ws-runs"',
                  'id="ws-model"', 'id="ws-grad"', 'id="ws-mins"', 'id="ws-amt"', 'id="ws-log"', 'id="ws-out"'):
        assert field in where, field
    assert 'data-lkmode="DIA"' in where and 'data-lkmode="DDA"' in where
    assert 'onsubmit="return lkSubmit(event)"' in where
    assert '<input type="file" id="ws-log" class="ws-vh" accept=".txt,.log,text/plain" onchange="lkFile(this.files)">' in where
    # the privacy line, under the form
    assert ("Runs entirely in your browser: nothing you type or drop is sent anywhere or stored, "
            "and nothing goes in the page address.") in where
    # the answer is read out as it changes
    assert '<div class="ws-out" id="ws-out" aria-live="polite">' in where


def test_no_preliminary_scaling_anywhere(client):
    """The mockup's 'Example (preliminary)' path (DIA-NN 2.7.0 library-free,
    N/S ≈ 0.913) is a refusal now, not an estimate: nothing is scaled."""
    html = _page(client)
    main = _main_script(html)
    lookup = main[main.index("// ── Where does my run sit? (B1"):main.index("// ── Charts ──")]
    where = html[html.index('<div class="section" id="where">'):html.index('<div class="section" id="ranges">')]
    for gone in ("preliminary", "Example (", "0.913", "EX.ratio", "N/S", "on STAN's scale", "wsBand", "__stanLookup"):
        assert gone not in lookup and gone not in html, gone
    # no engine-to-engine ratio from the research (library-free 0.80 and 0.91,
    # MBR 1.03-1.05, Spectronaut 0.65-0.79 and 1.56-1.74) is quoted where a
    # visitor could take it for a conversion
    for ratio in ("0.80", "0.91", "0.65", "0.79", "1.56", "1.74", "1.03", "1.05", "1.034", "28%", "1–4%", "about 1%"):
        assert ratio not in lookup and ratio not in where, ratio
    # 7: no claim that DIA-NN's default FDR changed at a given version
    for claim in ("2.5.0", "2.5 and later", "2.5's default", "before DIA-NN 2.5", "ge25"):
        assert claim not in lookup and claim not in where, claim
    assert "Don\\'t rescale your number by hand; calibration is planned." in lookup
    # no arithmetic turns the typed count into another number
    assert not re.search(r"\b(?:val|n\.v)\s*[*/]\s*[\d.]", lookup)


def test_lookup_code_makes_no_network_storage_or_address_call(client):
    main = _main_script(_page(client))
    lookup = main[main.index("// ── Where does my run sit? (B1"):main.index("// ── Charts ──")]
    for call in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "new Image", "import(",
                 "localStorage", "sessionStorage", "indexedDB", "document.cookie", "location", "history.",
                 "postMessage", "navigator.", "window.open"):
        assert call not in lookup, call
    # the file is read with FileReader only, and only its first 2 MB
    assert "const LK_LOG_MAX_BYTES = 2 * 1024 * 1024;" in lookup
    assert "r = new FileReader();" in lookup
    assert "r.readAsText(f.slice ? f.slice(0, LK_LOG_MAX_BYTES) : f);" in lookup


# Pinned from main fc5cb33 (relay 1.4.0). The P2a read-time rules (the D8
# dedupe, the >5,000 ng hold-back, colKey) and the B2 cohort key are untouched.
P2A_RULES = {
    "dedupe_heldback_js": "0c42bc64f1fba0d31f5bd3fd6aced720006bdb03a324ba0a2485ce0ec83f75b3",
    "colkey_js": "ef38cfffa182968c2943f074ad5d7f67b596922d45e50385cce02d931034719c",
    "cohort_key_js": "6f3c3aa443556346d7d7a56634fb7590df770ed133c568b5afef4cb429f4d2a8",
    # the view state and PANELS, with P2c's one 'lookup' line taken out
    "view_state_js": "fc3b5b7964de0e86a1383d9014312957cce263cba5371b01c6df6cb32bc8efb2",
}
LOOKUP_PANEL_LINE = "    ['lookup',         _NO_COLUMN,   () => renderLookup()],   // fields the visitor has not set follow the bar (P2c)\n"


def test_tic_peg_and_p2a_rules_are_byte_identical_to_fc5cb33(client):
    html = _page(client)
    got = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in _regions(html).items()}
    assert got == UNCHANGED      # TIC code and card, PEG section, CSS and script, esc(): unchanged since 1.3.0

    def between(a: str, b: str, incl: bool = False) -> str:
        i = html.index(a)
        j = html.index(b, i + len(a))
        return html[i:j + (len(b) if incl else 0)]
    view_state = between("const VIEW_DEFAULT = {", "function _countBy(rows, keyOf) {")
    assert view_state.count(LOOKUP_PANEL_LINE) == 1
    rules = {
        "dedupe_heldback_js": between("const DUP_WINDOW_MS = 2000;", "let submittedRows = 0;"),
        "colkey_js": between("function colKey(s) {", "\n}\n", True),
        "cohort_key_js": between("const EVOSEP_METHODS = {", "// ── One filter state for every panel (B2)"),
        "view_state_js": view_state.replace(LOOKUP_PANEL_LINE, ""),
    }
    assert {k: hashlib.sha256(v.encode()).hexdigest() for k, v in rules.items()} == P2A_RULES
    # Outside its own block the lookup adds exactly: the PANELS line, its call
    # in loadData, and its name among the panels renderCharts skips.
    main = _main_script(html)
    assert main.count("    try { renderLookup(); }   catch (e) { console.error('[renderLookup]', e); }\n") == 1
    assert main.count("const notCharts = new Set(['stats', 'lookup', 'ref-ranges', 'table']);") == 1


def test_real_log_fixture_text_is_what_the_parser_reads():
    """The fixtures are real DIA-NN output: the banner, command line and echo
    lines the parser relies on, as DIA-NN wrote them."""
    hive = (LOGS / "stan_hive_2.3.0_timstof_report.log.txt").read_text()
    # DIA-NN 2.x starts its log with a blank line (Linux and Windows); 1.9 did not
    assert hive.startswith("\nDIA-NN 2.3.0 Academia  (Data-Independent Acquisition by Neural Networks)\n")
    assert ("/diann-2.3.0/diann-linux --f /quobyte/proteomics-grp/STAN/incoming/TIMS-HOST/01132026_HE50_60-spd-dia_S1-A1_1_19167.d "
            "--lib /quobyte/proteomics-grp/STAN/TIMS-HOST/instrument_library.parquet") in hive
    assert "--threads 8 --qvalue 0.01 --min-pep-len 7 --max-pep-len 30 --missed-cleavages 1 --min-pr-charge 2 --max-pr-charge 4" in hive
    assert "\nOutput will be filtered at 0.01 FDR\n" in hive and "\n1 files will be processed\n" in hive
    assert "\n[2:03] IDs at 0.01 FDR: 23354\n[2:03] Number of IDs at 0.01 FDR: 23984\n" in hive
    pc = (LOGS / "stan_pc_2.3.2_exploris_report.log.txt").read_bytes()
    assert pc.startswith(b"\r\nDIA-NN 2.3.2 Academia  (Data-Independent Acquisition by Neural Networks)\r\n")
    assert b"--lib C:\\Users\\user\\STAN\\community_assets\\hela_orbitrap_202604.parquet --qvalue 0.01" in pc
    gui19 = (LOGS / "diann_1.9_gui_libfree_mbr_2files_head.log.txt").read_bytes()
    assert gui19.startswith(b"DIA-NN 1.9 (Data-Independent Acquisition by Neural Networks)\r\n")
    assert b"_30m.raw  --lib  --threads 32 " in gui19                      # the GUI's empty --lib
    assert b"--fasta-search" in gui19 and b"--reanalyse" in gui19
    assert b"\r\nLibrary-free search enabled\r\n" in gui19
    assert b"\r\nA spectral library will be created from the DIA runs and used to reanalyse them;" in gui19
    gui23 = (LOGS / "diann_2.3.0_gui_predicted_mbr_12files_head.log.txt").read_bytes()
    assert b"\r\nMBR enabled; .quant files will only be saved to disk during the first pass\r\n" in gui23
    assert b"\r\n12 files will be processed\r\n" in gui23
    v27 = (LOGS / "diann_2.7.0_predicted_4files_head.log.txt").read_text()
    assert v27.startswith("\nDIA-NN 2.7.0 Academia  (Data-Independent Acquisition by Neural Networks)\n")   # a blank first line
    assert "diann-linux --qvalue 0.01 --cut K*,R*" in v27 and "--lib /data/project/predicted.predicted.speclib" in v27


# ── page behaviour, in node ──────────────────────────────────────────

@needs_node
def test_matching_percentile_against_a_hand_computation(client, tmp_path):
    values = [30000 + 1000 * i for i in range(20)]          # 30,000 .. 49,000
    rows = _ht60(values)
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60'); {MATCH_DIA}
        const out = {{}};
        for (const v of ['41500', '41000', '38,000', '29999', '50000', '30000', '49000']) {{ lkSet('val', v); out[v] = els['ws-out'].innerHTML + els['ws-strip'].innerHTML; }}
        return out;
    }})()"""
    got = _run(client, tmp_path, scenario)
    med, q25, q75 = (_interp_quantile(values, p) for p in (0.5, 0.25, 0.75))
    assert (med, q25, q75) == (39500, 34750, 44250)
    for typed, val in [("41500", 41500), ("41000", 41000), ("38,000", 38000), ("30000", 30000), ("49000", 49000)]:
        p = _mid_rank_pct(val, values)
        expect = min(99, max(1, int(p + 0.5)))
        suffix = "th" if 11 <= expect % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(expect % 10, "th")
        t = _text(got[typed])
        assert f"{expect}{suffix} percentile" in t, (typed, p, t[:200])
        assert f"higher than {int(p + 0.5)}% of the 20 runs in this cohort" in t
        assert "Among the 20 runs in HeLa · timsTOF HT · DIA · Evosep 60 SPD · 50 ng" in t
        assert "Cohort median 39,500 precursors; middle half 34,750 – 44,250" in t
        assert "20 runs · 1 lab" in t and "single-lab reference" in t
        assert "not among labs" in t and "failed runs included" in t
        assert "Compared as the community search: DIA-NN 2.3.x · the frozen community library · this run alone, MBR off · 1% run-level FDR." in t
        assert 'id="ws-strip"' in got[typed] and f"You · {val:,}" in got[typed]
    # 41,500: 12 runs below -> 60th; 41,000: 11 below and one tie -> 57.5 -> 58th
    assert "60th percentile" in _text(got["41500"]) and "58th percentile" in _text(got["41000"])
    assert "Below every run" in _text(got["29999"]) and "below every one of the 20 runs" in _text(got["29999"])
    assert "Above every run" in _text(got["50000"]) and "above every one of the 20 runs" in _text(got["50000"])
    # timsTOF HT: the subset-library caveat (decision 11)
    assert "the full library gave a slightly higher count than the subset" in _text(got["41500"])
    # typed in, not read from a log: the page says the details are self-reported
    assert "The search details are as you entered them: self-reported, not checked." in _text(got["41500"])


@needs_node
def test_cohort_is_the_page_key_with_its_rules(client, tmp_path):
    """Same QC standard, model, mode, gradient and amount bucket as the cards;
    one copy per acquisition; held-back amounts out; ranked from 5 runs; below
    10 the values are listed; under 5 no percentile."""
    base = _ht60([40000 + 500 * i for i in range(7)])
    rows = base + [
        dict(base[0], submission_id="copy-of-0", display_name="Anonymous Lab"),     # a second copy of one acquisition
        _ht60([45000])[0] | {"submission_id": "held", "amount_ng": 100000},         # a unit error: held back
        _ht60([46000])[0] | {"submission_id": "s75", "amount_ng": 75, "n_peptides": 1},  # 75 ng is the 50 ng bucket
        _ht60([47000])[0] | {"submission_id": "s76", "amount_ng": 76, "n_peptides": 2},  # 76 ng is not
        _ht60([48000])[0] | {"submission_id": "k562", "sample_type": "k562", "n_peptides": 3},
        _ht60([49000])[0] | {"submission_id": "dda", "acquisition_mode": "dda", "n_psms": 30000, "n_peptides": 4},
        _ht60([49500])[0] | {"submission_id": "noLc", "lc_system": "", "n_peptides": 5},   # 60 SPD, no LC: "LC not recorded"
    ] + [_ht60([41000 + i])[0] | {"submission_id": f"e100-{i}", "spd": 100, "n_peptides": 100 + i} for i in range(3)]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60'); {MATCH_DIA} lkSet('val', '42000');
        const ht60 = els['ws-out'].innerHTML;
        const card = cohortsOf(viewRows()).filter(c => c.key === 'hela|timsTOF HT|DIA|evosep:60|50').map(c => c.rows.length)[0];
        lkSet('grad', 'evosep:100'); const e100 = els['ws-out'].innerHTML;
        lkSet('amt', '100_250'); const amt = els['ws-out'].innerHTML;
        return {{ ht60, card, e100, amt }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    raw_e100 = got["e100"]
    got = {k: (_text(v) if isinstance(v, str) else v) for k, v in got.items()}
    # 7 + the 75 ng run = 8; the copy, the held-back, 76 ng, K562, DDA and no-LC rows are not in it
    assert got["card"] == 8
    t = got["ht60"]
    assert "Among the 8 runs in HeLa · timsTOF HT · DIA · Evosep 60 SPD · 50 ng" in t
    assert "Only 8 runs: one more run can move a percentile by several points." in t
    assert "The 8 runs: 40,000 · 40,500 · 41,000 · 41,500 · 42,000 · 42,500 · 43,000 · 46,000 precursors (median 41,750)." in t
    assert "56th percentile" in t      # 4 below + a tie: (4 + 0.5) / 8 = 56.25
    # Evosep 100 holds 3 runs: no percentile, the values, and how many are below
    e = got["e100"]
    assert "Too few runs for a percentile" in e and "<small>percentile</small>" not in raw_e100
    assert "This cohort is not ranked: fewer than 5 runs." in e
    assert "Its 3 runs: 41,000 · 41,001 · 41,002 precursors. Yours is above 3 of them." in e
    # 76-250 ng at Evosep 100: none; the same gradient at another amount is offered
    a = got["amt"]
    assert "No cohort yet" in a and "No lab has shared timsTOF HT DIA runs at Evosep 100 SPD with 100–250 ng loaded." in a
    assert "Same gradient at another amount: 26–75 ng (the 50 ng standard) · 3 runs" in a


@needs_node
def test_no_cohort_names_unranked_runs_and_the_nearest_cohort(client, tmp_path):
    rows = ([_dda(i, spd=60, lc_system="", gradient_length_min=21) for i in range(13)]          # DDA, 60 SPD, no LC
            + [_dda(100 + i) for i in range(6)])                                                 # DDA, Evosep 100
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        {MATCH_DDA} lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60'); lkSet('val', '30500');
        return els['ws-out'].innerHTML;
    }})()"""
    html = _run(client, tmp_path, scenario)
    t = _text(html)
    assert "No cohort yet" in t
    assert "No lab has shared timsTOF HT DDA runs at Evosep 60 SPD with 50 ng loaded." in t
    assert ("13 runs at 60 SPD exist but are not ranked: no LC recorded, and 60 SPD is also an Evosep method, "
            "so Evosep or nanoLC is unknown.") in t
    assert "Nearest ranked timsTOF HT cohort at 50 ng: Evosep 100 SPD · 6 runs. It is a different gradient, so no percentile is given against it." in t
    assert "<small>percentile</small>" not in html


@needs_node
def test_nanolc_minutes_map_to_the_nearest_cohort(client, tmp_path):
    rows = [_nano(i) for i in range(12)]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('model', 'Orbitrap Exploris 480'); {MATCH_DIA} lkSet('val', '22050');
        const listed = els['ws-out'].innerHTML, gradOpts = els['ws-grad'].innerHTML;
        lkSet('grad', 'nano'); const empty = els['ws-out'].innerHTML, minsShown = !els['ws-mins-f'].classList.contains('ws-hidden');
        lkSet('mins', '30'); const m30 = els['ws-out'].innerHTML;
        lkSet('mins', '50'); const m50 = els['ws-out'].innerHTML;
        return {{ listed, gradOpts, empty, minsShown, m30, m50 }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert '<option value="nanolc:38" selected>~30 min gradient (38 SPD) · 44 min run · 12 runs</option>' in got["gradOpts"]
    assert '<option value="nano">Another gradient: enter its length</option>' in got["gradOpts"]
    assert "Among the 12 runs in HeLa · Orbitrap Exploris 480 · DIA · ~30 min gradient (38 SPD) · 44 min run · 50 ng" in _text(got["listed"])
    assert got["minsShown"] and "Enter your gradient length" in _text(got["empty"])
    t30 = _text(got["m30"])
    # 1440 / (30 × 1.25) = 38.4 SPD: the 38 SPD cohort, within 15%
    assert "Your 30 min gradient ≈ 38 SPD (1440 ÷ (30 × 1.25))." in t30 and "Nearest cohort: ~30 min gradient (38 SPD) · 44 min run." in t30
    assert "Among the 12 runs in HeLa · Orbitrap Exploris 480" in t30 and "percentile" in t30
    # Exploris: the subset-library caveat without a timsTOF number
    assert "compares closely but not exactly" in t30 and "1.034" not in t30
    t50 = _text(got["m50"])
    # 1440 / (50 × 1.25) = 23 SPD: 38 is 50% away, so no cohort
    assert "Your 50 min gradient ≈ 23 SPD" in t50 and "No nanoLC cohort of this instrument is within 15% of it." in t50
    assert "No cohort yet" in t50 and "Nearest ranked Orbitrap Exploris 480 cohort at 50 ng: ~30 min gradient (38 SPD) · 44 min run · 12 runs." in t50


@needs_node
def test_defaults_follow_the_bar_until_set_here(client, tmp_path):
    rows = ([_row(i) for i in range(6)] + _ht60([45000 + i for i in range(9)])
            + [_nano(500 + i) for i in range(7)] + [_dda(600 + i) for i in range(5)])
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        const snap = () => ({{ mode: lk.mode, model: lk.model, grad: lk.grad, amt: lk.amt, view: {{ ...view }},
                               modelSel: els['ws-model'].value, gradSel: els['ws-grad'].value, amtSel: els['ws-amt'].value }});
        const out = {{ start: snap() }};
        setView({{ model: 'Orbitrap Exploris 480' }}); out.exploris = snap();
        setView({{ model: 'timsTOF HT', gradient: 'evosep:100' }}); out.ht100 = snap();
        setView({{ amount: 'all' }}); out.all = snap();
        setView({{ mode: 'dda', model: '', gradient: '', amount: '50' }}); out.dda = snap();
        setView({{ mode: 'all' }}); out.both = snap();
        // set here: the bar no longer moves these, and the lookup never moves the bar
        setView({{ mode: 'dia' }});
        const before = {{ ...view }};
        lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60'); lkSet('amt', 'le25'); lkSet('val', '41000'); {MATCH_DIA}
        out.viewUnchanged = JSON.stringify(view) === JSON.stringify(before);
        setView({{ model: 'Orbitrap Exploris 480' }}); out.owned = snap();
        lkClear(); out.cleared = snap(); out.clearedSearch = {{ ...lk.DIA }};
        return out;
    }})()"""
    got = _run(client, tmp_path, scenario)
    s = got["start"]
    # HeLa · DIA · 50 ng; the instrument with the most DIA runs and its largest cohort
    assert (s["mode"], s["model"], s["grad"], s["amt"]) == ("DIA", "timsTOF HT", "evosep:60", "50")
    assert (s["modelSel"], s["gradSel"], s["amtSel"]) == ("timsTOF HT", "evosep:60", "50")
    assert (got["exploris"]["model"], got["exploris"]["grad"]) == ("Orbitrap Exploris 480", "nanolc:38")
    assert (got["ht100"]["model"], got["ht100"]["grad"]) == ("timsTOF HT", "evosep:100")
    assert got["all"]["amt"] == "50"                                   # "All amounts" is not a lookup amount
    assert (got["dda"]["mode"], got["dda"]["model"], got["dda"]["grad"]) == ("DDA", "timsTOF HT", "evosep:100")
    assert got["both"]["mode"] == "DDA"                                # "Both" keeps the lookup's mode
    assert got["viewUnchanged"] is True
    o = got["owned"]
    assert (o["model"], o["grad"], o["amt"]) == ("timsTOF HT", "evosep:60", "le25") and o["view"]["model"] == "Orbitrap Exploris 480"
    c = got["cleared"]
    assert (c["model"], c["grad"], c["amt"]) == ("Orbitrap Exploris 480", "nanolc:38", "50")
    assert got["clearedSearch"] == {"val": "", "eng": "", "ver": "", "lib": "", "fdr": "", "runs": ""}


@needs_node
def test_search_fields_start_unchosen_and_ask(client, tmp_path):
    """Nothing is assumed about the search: until the visitor says (or drops
    the log), the count is not placed, though the cohort is shown."""
    rows = _ht60([30000 + 1000 * i for i in range(20)])
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('val', '41500');
        const start = [els['ws-out'].innerHTML, els['ws-eng'].value, els['ws-eng'].innerHTML];
        lkSet('eng', 'diann');
        const eng = els['ws-out'].innerHTML;
        return {{ start, eng }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    html, eng_value, eng_opts = got["start"]
    t = _text(html)
    assert eng_value == "" and eng_opts.startswith('<option value="" selected>Choose…</option>')
    assert "How was it searched?" in t and "<small>percentile</small>" not in html
    assert "Choose the search engine above, or drop the DIA-NN log, to place 41,500 precursors." in t
    assert "Your cohort: the 20 runs in HeLa · timsTOF HT · DIA · Evosep 60 SPD · 50 ng" in t
    assert "You ·" not in html and 'id="ws-strip"' in html             # the cohort, without a marker
    assert "Choose the version, the library, the FDR, whether it was searched alone, and MBR above" in _text(got["eng"])


REFUSALS = [
    # (fields, mode, what the answer must say)
    ({"eng": "sn"}, "DIA", "Spectronaut is not the community search, and its directDIA counts move with the size of the experiment"),
    ({"eng": "other"}, "DIA", "Another engine is not the community search, and no conversion between engines is published yet."),
    ({"eng": "diann", "ver": "1.9", "lib": "frozen", "fdr": "run1", "runs": "alone"}, "DIA",
     "DIA-NN 1.9 is not the community version, 2.3.x. Scoring, calibration and defaults change between versions;"),
    ({"eng": "diann", "ver": "2.7", "lib": "frozen", "fdr": "run1", "runs": "alone"}, "DIA",
     "current DIA-NN filters the main report at 5% FDR by default, while the community count is at 1%."),
    ({"eng": "diann", "ver": "2.3", "lib": "free", "fdr": "run1", "runs": "alone"}, "DIA",
     "Library-free or predicted (from a FASTA, --predictor, or a .predicted.speclib): the community search uses the frozen empirical library"),
    ({"eng": "diann", "ver": "2.3", "lib": "own", "fdr": "run1", "runs": "alone"}, "DIA",
     "A library from your own runs, or a project or public library: its size and content set the ceiling"),
    ({"eng": "diann", "ver": "2.3", "lib": "frozen", "fdr": "run1", "runs": "mbr1"}, "DIA",
     "Match-between-runs (--reanalyse): a second pass searches the run against a library built from the search's own runs"),
    ({"eng": "diann", "ver": "2.3", "lib": "frozen", "fdr": "run1", "runs": "mbrN"}, "DIA", "Match-between-runs (--reanalyse)"),
    ({"eng": "diann", "ver": "2.3", "lib": "frozen", "fdr": "global1", "runs": "alone"}, "DIA",
     "A global 1% filter (Global.Q.Value as well as Q.Value): STAN counts at run-level Q.Value ≤ 0.01 only"),
    ({"eng": "diann", "ver": "2.3", "lib": "frozen", "fdr": "other", "runs": "alone"}, "DIA",
     "Not 1% run-level FDR: STAN counts precursors at Q.Value ≤ 0.01. A report filtered at a looser level, such as current DIA-NN's 5% default, holds more precursors."),
    ({"eng": "other"}, "DDA", "Another DDA engine is not the community search"),
    ({"eng": "sage", "ver": "other", "lib": "frozen", "fdr": "psm1"}, "DDA", "Another Sage version: the community DDA search is Sage 0.14.x."),
    ({"eng": "sage", "ver": "0.14", "lib": "own", "fdr": "psm1"}, "DDA", "Another FASTA: the size of the database changes the PSM FDR."),
    ({"eng": "sage", "ver": "0.14", "lib": "frozen", "fdr": "other"}, "DDA", "Not 1% PSM-level FDR"),
]


@needs_node
def test_each_refusal_names_the_difference_and_the_way_to_compare(client, tmp_path):
    rows = _ht60([30000 + 1000 * i for i in range(20)]) + [_dda(100 + i) for i in range(6)]
    cases = json.dumps([[f, m] for f, m, _ in REFUSALS])
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        return {cases}.map(([f, mode]) => {{
            lkClear(); lkSet('mode', mode); lkSet('model', 'timsTOF HT'); lkSet('grad', mode === 'DIA' ? 'evosep:60' : 'evosep:100');
            for (const [k, v] of Object.entries(f)) lkSet(k, v);
            lkSet('val', mode === 'DIA' ? '41500' : '30200');
            return els['ws-out'].innerHTML;
        }});
    }})()"""
    got = _run(client, tmp_path, scenario)
    for (fields, mode, why), html in zip(REFUSALS, got):
        t = _text(html)
        assert why in t, (fields, t[:600])
        assert "Can't compare yet" in t and "It is not placed in a cohort, because:" in t
        assert "<small>percentile</small>" not in html, fields                  # never a percentile
        assert "You ·" not in html                                               # never a marker on the strip
        if mode == "DIA":
            assert ("To place this run, search the raw file again with DIA-NN 2.3.x, on its own and with MBR off, "
                    "against hela_timstof_202604.parquet and human_hela_202604.fasta from the dataset, as STAN does: "
                    "--lib hela_timstof_202604.parquet --fasta human_hela_202604.fasta --qvalue 0.01 --min-pep-len 7 "
                    "--max-pep-len 30 --missed-cleavages 1 --min-pr-charge 2 --max-pr-charge 4 Then drop its report.log.txt here") in t
            # each flag stays on one line at phone width
            assert '<span>--missed-cleavages 1</span> <span>--min-pr-charge 2</span>' in html
            assert "Converting counts from other engines, versions and libraries is planned" in t and "Until then nothing is scaled." in t
            assert "Don't rescale your number by hand; calibration is planned." in t
            assert "You entered 41,500 precursors" in t
            assert "Your cohort would be the 20 runs in HeLa · timsTOF HT · DIA · Evosep 60 SPD · 50 ng" in t
            assert '/stan-benchmark/blob/main/community_library/hela_timstof_202604.parquet"' in html
        else:
            assert "To place this run, search the raw file with Sage 0.14.x against human_hela_202604.fasta" in t
            assert "DDA PSM counts are not covered yet. Nothing is scaled." in t
            assert "You entered 30,200 PSMs" in t
    # the configuration is named back in plain words
    assert "(DIA-NN 1.9 · the frozen community library · this run alone, MBR off · 1% run-level FDR)" in _text(got[2])


@needs_node
def test_matches_and_their_notes(client, tmp_path):
    rows = _ht60([30000 + 1000 * i for i in range(20)]) + [_dda(i) for i in range(6)]       # 30,000..30,500 PSMs
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60'); {MATCH_DIA} lkSet('runs', 'batch'); lkSet('val', '41500');
        const batch = els['ws-out'].innerHTML;
        lkSet('val', 'forty thousand'); const bad = els['ws-out'].innerHTML;
        lkSet('val', ''); const empty = els['ws-out'].innerHTML;
        {MATCH_DDA} lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:100'); lkSet('val', '30250');
        const dda = els['ws-out'].innerHTML;
        return {{ batch, bad, empty, dda, label: els['ws-val-l'].textContent, libLabel: els['ws-lib-l'].textContent, runsHidden: els['ws-runs-f'].classList.contains('ws-hidden'), dropHidden: els['ws-drop-f'].classList.contains('ws-hidden') }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    b = _text(got["batch"])
    assert "60th percentile" in b
    assert "Searched with other runs, MBR off: each run is still identified on its own, but unless the mass accuracy is fixed DIA-NN tunes it on the first run of the batch" in b
    assert "Enter a whole number" in _text(got["bad"]) and "You ·" not in got["bad"]
    assert "Enter your precursors" in _text(got["empty"])
    d = _text(got["dda"])
    # 6 DDA runs at 30,000..30,500 PSMs; 30,250: 3 below -> 100 × 3 / 6 = 50 -> 50th
    assert "50th percentile" in d and "Among the 6 runs in HeLa · timsTOF HT · DDA · Evosep 100 SPD · 50 ng" in d
    assert "Compared as the community search: Sage 0.14.x · the frozen community FASTA · 1% PSM-level FDR." in d
    assert "subset" not in d                                             # the library caveat is DIA only
    assert got["label"] == "PSMs at 1% FDR" and got["libLabel"] == "Database (FASTA)"
    assert got["runsHidden"] is True and got["dropHidden"] is True


def _log_scenario(rows: list[dict], name: str, model: str, grad: str) -> str:
    text = (LOGS / name).read_bytes().decode()
    return f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('model', {json.dumps(model)}); lkSet('grad', {json.dumps(grad)});
        lkFile([fakeFile({json.dumps(text)})]);
        return {{ parsed: lkLog, msg: els['ws-log-out'].innerHTML, out: els['ws-out'].innerHTML, reads,
                  fields: ['ws-eng', 'ws-ver', 'ws-lib', 'ws-fdr', 'ws-runs', 'ws-val'].map(id => els[id].value) }};
    }})()"""


@needs_node
def test_real_logs_parse_to_the_right_fields(client, tmp_path):
    scenario = f"""(() => {{
        const logs = {json.dumps({p.name: p.read_bytes().decode() for p in sorted(LOGS.glob("*.txt"))})};
        const keep = ['ok', 'version', 'verKey', 'libName', 'libKind', 'libVendor', 'instrumentLib', 'fastaSearch', 'mbr', 'qvalue', 'fdr', 'nfiles', 'runs', 'count', 'libLoaded', 'libCheck'];
        return Object.fromEntries(Object.entries(logs).map(([k, t]) => {{ const o = parseDiannLog(t); return [k, Object.fromEntries(keep.map(f => [f, o[f]]))]; }}));
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert got["stan_hive_2.3.0_timstof_report.log.txt"] == {
        "ok": True, "version": "2.3.0", "verKey": "2.3", "libName": "instrument_library.parquet", "libKind": "own", "libVendor": None,
        "instrumentLib": True, "fastaSearch": False, "mbr": False, "qvalue": 0.01, "fdr": "run1", "nfiles": 1, "runs": "alone", "count": 23984,
        "libLoaded": 51487, "libCheck": None}
    assert got["stan_pc_2.3.2_exploris_report.log.txt"] == {
        "ok": True, "version": "2.3.2", "verKey": "2.3", "libName": "hela_orbitrap_202604.parquet", "libKind": "frozen", "libVendor": "thermo",
        "instrumentLib": False, "fastaSearch": False, "mbr": False, "qvalue": 0.01, "fdr": "run1", "nfiles": 1, "runs": "alone", "count": 25336,
        "libLoaded": 170284, "libCheck": "name+size"}
    assert got["diann_2.7.0_predicted_4files_head.log.txt"] == {
        "ok": True, "version": "2.7.0", "verKey": "2.7", "libName": "predicted.predicted.speclib", "libKind": "free", "libVendor": None,
        "instrumentLib": False, "fastaSearch": False, "mbr": False, "qvalue": 0.01, "fdr": "run1", "nfiles": 4, "runs": "batch", "count": None,
        "libLoaded": 4284564, "libCheck": None}
    assert got["diann_2.3.0_gui_predicted_mbr_12files_head.log.txt"] == {
        "ok": True, "version": "2.3.0", "verKey": "2.3", "libName": "lib.predicted.speclib", "libKind": "free", "libVendor": None,
        "instrumentLib": False, "fastaSearch": False, "mbr": True, "qvalue": 0.01, "fdr": "run1", "nfiles": 12, "runs": "mbrN", "count": None,
        "libLoaded": 4338771, "libCheck": None}
    assert got["diann_1.9_gui_libfree_mbr_2files_head.log.txt"] == {
        "ok": True, "version": "1.9", "verKey": "1.9", "libName": None, "libKind": "free", "libVendor": None,
        "instrumentLib": False, "fastaSearch": True, "mbr": True, "qvalue": 0.01, "fdr": "run1", "nfiles": 2, "runs": "mbrN", "count": None,
        "libLoaded": None, "libCheck": None}


@needs_node
def test_dropping_stan_pc_log_places_an_exploris_run(client, tmp_path):
    """STAN's Windows install searched with DIA-NN 2.3.2 and the frozen Orbitrap
    library: the community search. The log fills every field and the count."""
    rows = [_nano(i, n_precursors=24000 + 200 * i) for i in range(15)]
    got = _run(client, tmp_path, _log_scenario(rows, "stan_pc_2.3.2_exploris_report.log.txt", "Orbitrap Exploris 480", "nanolc:38"))
    assert got["fields"] == ["diann", "2.3", "frozen", "run1", "alone", "25336"]
    assert got["reads"] == [[0, 2 * 1024 * 1024]]                       # only the first 2 MB were asked for
    msg = _text(got["msg"])
    assert msg == ("Read from the log: DIA-NN 2.3.2 · hela_orbitrap_202604.parquet, the frozen Orbitrap community library "
                   "(its name and size, 170,284 precursors, match; not checked by checksum) · "
                   "1 file, MBR off · precursor FDR 1% · 25,336 precursors, the last “Number of IDs at 0.01 FDR” line.")
    t = _text(got["out"])
    # 24,000..26,800: 25,336 has 7 runs below (24,000..25,200) -> 7/15 = 46.7 -> 47th
    assert "47th percentile" in t and "Among the 15 runs in HeLa · Orbitrap Exploris 480 · DIA · ~30 min gradient (38 SPD) · 44 min run · 50 ng" in t
    assert "Compared as the community search: DIA-NN 2.3.2 · the frozen community library · this run alone, MBR off · 1% run-level FDR." in t
    assert "The library was checked from your log by file name and size (170,284 precursors loaded), not by checksum." in t


@needs_node
def test_dropping_other_real_logs_refuses_with_the_reason(client, tmp_path):
    rows = _ht60([30000 + 1000 * i for i in range(20)])
    hive = _run(client, tmp_path, _log_scenario(rows, "stan_hive_2.3.0_timstof_report.log.txt", "timsTOF HT", "evosep:60"))
    assert hive["fields"] == ["diann", "2.3", "own", "run1", "alone", "23984"]
    t = _text(hive["out"])
    assert "Can't compare yet" in t and "STAN's instrument_library.parquet is built from your own lab's runs, not the frozen community library." in t
    assert "built from your own lab's runs" in _text(hive["msg"])
    gui = _run(client, tmp_path, _log_scenario(rows, "diann_1.9_gui_libfree_mbr_2files_head.log.txt", "timsTOF HT", "evosep:60"))
    assert gui["fields"][:5] == ["diann", "1.9", "free", "run1", "mbrN"]
    t = _text(gui["out"])
    for why in ("DIA-NN 1.9 is not the community version", "Library-free or predicted", "Match-between-runs (--reanalyse)"):
        assert why in t
    assert "several runs in one log: enter this run's own count" in _text(gui["msg"])
    v27 = _run(client, tmp_path, _log_scenario(rows, "diann_2.7.0_predicted_4files_head.log.txt", "timsTOF HT", "evosep:60"))
    t = _text(v27["out"])
    assert "DIA-NN 2.7.0 is not the community version" in t and "a library predicted from a FASTA: library-free" in _text(v27["msg"])


@needs_node
def test_a_chosen_instrument_of_the_other_vendor_asks_for_the_right_one(client, tmp_path):
    rows = _ht60([30000 + 1000 * i for i in range(20)])
    text = (LOGS / "stan_pc_2.3.2_exploris_report.log.txt").read_bytes().decode()
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60');
        lkApplyLog(parseDiannLog({json.dumps(text)}), false);
        return els['ws-out'].innerHTML;
    }})()"""
    html = _run(client, tmp_path, scenario)
    t = _text(html)
    assert "Pick the instrument you ran" in t
    assert ("Your log searched hela_orbitrap_202604.parquet, the Orbitrap library, but the instrument chosen is timsTOF HT. "
            "Pick the instrument you ran.") in t
    assert "not the community search" not in t and "Can't compare yet" not in t and "<small>percentile</small>" not in html


HOSTILE_LOGS = {
    "lib_tag": "DIA-NN 2.3.0 Academia\nLogical CPU cores: 8\ndiann-linux --f a.d --lib /x/" + EVIL + ".parquet --qvalue 0.01\n\n1 files will be processed\n[0:01] Number of IDs at 0.01 FDR: 25000\n",
    "banner_tag": "DIA-NN <script>alert(1)</script>\ndiann-linux --f a.d --lib hela_timstof_202604.parquet\n",
    "version_tail": "DIA-NN 2.3.0" + EVIL + "\ndiann-linux --f a.d --lib hela_timstof_202604.parquet --qvalue 0.01\n",
    "qvalue_tag": "DIA-NN 2.3.0 Academia\ndiann-linux --f a.d --lib hela_timstof_202604.parquet --qvalue 0.01" + EVIL + "\n\n1 files will be processed\n",
    "huge_count": "DIA-NN 2.3.0\ndiann-linux --f a.d --lib hela_timstof_202604.parquet --qvalue 0.01\n\n1 files will be processed\n[0:01] Number of IDs at 0.01 FDR: 99999999999\n",
    "proto": "DIA-NN 2.3.0\ndiann-linux --f a.d --lib __proto__ --qvalue 0.01\n\n1 files will be processed\n",
    "long_name": "DIA-NN 2.3.0\ndiann-linux --f a.d --lib /p/" + "A" * 1000 + ".parquet --qvalue 0.01\n\n1 files will be processed\n",
    "overlong_line": "DIA-NN 2.3.0\ndiann-linux --f a.d --lib /p/" + "A" * 5000 + ".parquet --qvalue 0.01\n\n1 files will be processed\n",
    "binary": "\x00\x01PK\x03\x04\x00DIA-NN 2.3.0\x00\xff\xfe",
    "not_a_log": "Precursor.Id\tQ.Value\nAAAK2\t0.001\n",
    "no_version_24": "DIA-NN 2.4.1 Academia\ndiann-linux --f a.d --lib hela_timstof_202604.parquet --qvalue 0.01\n\n1 files will be processed\n",
}


@needs_node
def test_hostile_log_contents_are_contained(client, tmp_path):
    rows = _ht60([30000 + 1000 * i for i in range(20)])
    huge = "DIA-NN 2.3.0\n" + "x" * (5 * 1024 * 1024) + "\n"
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60');
        const out = {{}};
        for (const [k, t] of Object.entries({json.dumps(HOSTILE_LOGS)})) {{
            lkClear(); lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60');
            lkFile([fakeFile(t)]);
            out[k] = {{ parsed: lkLog, msg: els['ws-log-out'].innerHTML, html: els['ws-out'].innerHTML + els['ws-log-out'].innerHTML + els['ws-ver'].innerHTML + els['ws-lib'].innerHTML }};
        }}
        const t0 = Date.now(); const big = parseDiannLog({json.dumps(huge)}); out.huge = {{ ms: Date.now() - t0, ok: big.ok, cmd: big.cmd }};
        lkClear(); lkFile([fakeFile({json.dumps(huge)})]); out.hugeMsg = els['ws-log-out'].innerHTML; out.reads = reads.slice(-1);
        return out;
    }})()"""
    got = _run(client, tmp_path, scenario)
    for k in HOSTILE_LOGS:
        html = got[k]["html"]
        assert "<img" not in html and "<script" not in html and "onerror=\"" not in html, k
    # a tag inside the library's name is escaped, and the library is not the frozen one
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;.parquet" in got["lib_tag"]["msg"]
    assert got["lib_tag"]["parsed"]["libKind"] == "own" and got["lib_tag"]["parsed"]["count"] == 25000
    # no "DIA-NN x.y" banner: nothing is read or changed
    for k in ("banner_tag", "version_tail", "binary", "not_a_log"):
        assert got[k]["parsed"] is None, k
        assert "This does not look like a DIA-NN log" in got[k]["msg"], k
    # values that do not fit their pattern are dropped, not echoed
    assert got["qvalue_tag"]["parsed"]["qvalue"] is None and got["qvalue_tag"]["parsed"]["fdr"] == ""     # no FDR stated: the visitor chooses
    assert got["huge_count"]["parsed"]["count"] is None
    assert got["proto"]["parsed"]["libKind"] == "own" and got["proto"]["parsed"]["libVendor"] is None
    assert len(got["long_name"]["parsed"]["libName"]) == 120
    # a line over 4,096 characters is never read
    assert got["overlong_line"]["parsed"]["libName"] is None and got["overlong_line"]["parsed"]["cmd"] is False
    assert got["no_version_24"]["parsed"]["verKey"] == "unk"           # there is no DIA-NN 2.4: never "2.3"
    # a 5 MB file: parsed within its 2 MB cap, quickly, and only 2 MB asked for
    assert got["huge"]["ok"] is True and got["huge"]["ms"] < 2000
    assert got["reads"] == [[0, 2 * 1024 * 1024]] and "Only the first 2 MB of the file was read." in got["hugeMsg"]


@needs_node
def test_hostile_names_in_the_data_are_escaped(client, tmp_path):
    rows = [_row(i, instrument_model=EVIL, instrument_family=EVIL, spd=60, gradient_length_min=21,
                 n_precursors=40000 + i, display_name=EVIL) for i in range(8)]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('model', {json.dumps(EVIL)}); lkSet('grad', 'evosep:60'); {MATCH_DIA} lkSet('val', '40003');
        const placed = els['ws-out'].innerHTML + els['ws-model'].innerHTML;
        lkSet('grad', 'evosep:100'); const none = els['ws-out'].innerHTML;
        lkSet('eng', 'sn'); const refused = els['ws-out'].innerHTML;
        return {{ placed, none, refused }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    for k, html in got.items():
        assert "<img" not in html, k
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in got["placed"] and "percentile" in got["placed"]


@needs_node
def test_no_network_call_on_any_interaction(client, tmp_path):
    """Typing, choosing, submitting, dropping a log, clearing and showing the
    card: none of it reaches the network (the harness records fetch, XHR,
    beacons, sockets, event streams and image loads), and none of it throws
    for want of browser storage or an address bar."""
    rows = _ht60([30000 + 1000 * i for i in range(20)])
    text = (LOGS / "stan_pc_2.3.2_exploris_report.log.txt").read_bytes().decode()
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar();
        net.length = 0;                               // the page's own /api/leaderboard load
        renderLookup();
        lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60'); {MATCH_DIA} lkSet('val', '41500');
        let prevented = false;
        const ret = lkSubmit({{ preventDefault() {{ prevented = true; }} }});
        lkFile([fakeFile({json.dumps(text)})]);
        lkDrop({{ preventDefault() {{}}, dataTransfer: {{ files: [fakeFile({json.dumps(text)})] }} }});
        lkSet('eng', 'sn'); lkSet('mode', 'DDA'); lkSet('mode', 'DIA');
        lkShowCard(); lkClear();
        return {{ prevented, ret, net: net.slice() }};
    }})()"""
    got = _run(client, tmp_path, scenario, allow_load_fetch=False)
    assert got == {"prevented": True, "ret": False, "net": []}


@needs_node
def test_show_card_sets_the_bar_to_the_cohort(client, tmp_path):
    rows = _ht60([30000 + 1000 * i for i in range(20)]) + [_row(i) for i in range(6)]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60');
        lkShowCard();
        return {{ view: {{ ...view }}, lk: [lk.model, lk.grad, lk.amt] }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert got["view"] == {"sample": "hela", "mode": "dia", "model": "timsTOF HT", "gradient": "evosep:60", "amount": "50", "column": ""}
    assert got["lk"] == ["timsTOF HT", "evosep:60", "50"]


@needs_node
def test_page_loads_with_storage_blocked_and_the_lookup_renders(client, tmp_path):
    rows = _ht60([30000 + 1000 * i for i in range(20)]) + [_nano(500 + i) for i in range(7)]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)});
        renderFilterBar(); updateStats(); renderTable(); renderLookup(); renderRefRanges(); renderCharts();
        renderPanels(null);
        return [els['ws-out'].innerHTML.includes('Your cohort: the 20 runs'), els['ws-model'].options.length > 5];
    }})()"""
    assert _run(client, tmp_path, scenario) == [True, True]


# ── review fixes (2026-10-01) ────────────────────────────────────────

def _log(lib: str, *, loaded: int | None = None, extra: str = "", qvalue: str = "0.01", count: int = 41000) -> str:
    """A one-file DIA-NN 2.3.0 log in the shape of the real ones."""
    loaded_line = (f"[0:00] Spectral library loaded: 6091 protein isoforms, 5959 protein groups and {loaded} precursors "
                   "in 49826 elution groups.\n") if loaded is not None else ""
    return ("\nDIA-NN 2.3.0 Academia  (Data-Independent Acquisition by Neural Networks)\nLogical CPU cores: 8\n"
            f"/diann-2.3.0/diann-linux --f /data/run.d --lib /data/{lib} --fasta /data/human_hela_202604.fasta "
            f"--threads 8 --qvalue {qvalue} {extra}\n\nOutput will be filtered at {qvalue} FDR\n\n1 files will be processed\n"
            f"[0:00] Loading spectral library /data/{lib}\n{loaded_line}"
            f"[2:03] IDs at 0.01 FDR: {count - 400}\n[2:03] Number of IDs at 0.01 FDR: {count}\n")


@needs_node
def test_frozen_library_is_checked_by_name_and_size(client, tmp_path):
    """Item 2: a library renamed to the frozen file name is caught by the
    number of precursors DIA-NN loaded from it (53,580 for the timsTOF
    library, from real DIA-NN 2.3.2 logs on Hive); a log without the size
    line is matched by name only, and the page says which."""
    rows = _ht60([30000 + 1000 * i for i in range(20)])
    logs = {"real": _log("hela_timstof_202604.parquet", loaded=53580),
            "renamed": _log("hela_timstof_202604.parquet", loaded=51487),
            "name_only": _log("hela_timstof_202604.parquet")}
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        const out = {{}};
        for (const [k, t] of Object.entries({json.dumps(logs)})) {{
            lkClear(); lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60');
            lkApplyLog(parseDiannLog(t), false);
            out[k] = {{ kind: lkLog.libKind, check: lkLog.libCheck, out: els['ws-out'].innerHTML, msg: els['ws-log-out'].innerHTML }};
        }}
        return out;
    }})()"""
    got = _run(client, tmp_path, scenario)
    real, renamed, name_only = got["real"], got["renamed"], got["name_only"]
    assert (real["kind"], real["check"]) == ("frozen", "name+size")
    t = _text(real["out"])
    assert "58th percentile" in t      # 41,000 in 30,000..49,000: 11 below and one tie
    assert "The library was checked from your log by file name and size (53,580 precursors loaded), not by checksum." in t
    assert "(its name and size, 53,580 precursors, match; not checked by checksum)" in _text(real["msg"])
    assert renamed["kind"] == "own" and renamed["check"] is None
    t = _text(renamed["out"])
    assert "Can't compare yet" in t and "<small>percentile</small>" not in renamed["out"]
    assert ("hela_timstof_202604.parquet here loaded 51,487 precursors, but the frozen library holds 53,580: "
            "it is another library under the frozen library's name.") in t
    assert "another library under that name" in _text(renamed["msg"])
    assert (name_only["kind"], name_only["check"]) == ("frozen", "name")
    t = _text(name_only["out"])
    assert "58th percentile" in t
    assert "The library was matched from your log by file name only (the log does not say how many precursors it loaded), not by checksum." in t
    assert "(matched by name only: the log does not say how many precursors it loaded)" in _text(name_only["msg"])


@needs_node
def test_predicted_spectra_with_the_frozen_library_are_refused(client, tmp_path):
    """Item 3: --predictor (or its echo line) means the spectra are predicted,
    not the frozen empirical library's, whatever --lib names."""
    rows = _ht60([30000 + 1000 * i for i in range(20)])
    flag = _log("hela_timstof_202604.parquet", loaded=53580, extra="--predictor")
    echo = _log("hela_timstof_202604.parquet", loaded=53580).replace(
        "\nOutput will be filtered", "\nDeep learning will be used to generate a new in silico spectral library from peptides provided\nOutput will be filtered")
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        return [{json.dumps(flag)}, {json.dumps(echo)}].map(t => {{
            lkClear(); lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60');
            lkApplyLog(parseDiannLog(t), false);
            return {{ kind: lkLog.libKind, predictor: lkLog.predictor, out: els['ws-out'].innerHTML, msg: els['ws-log-out'].innerHTML }};
        }});
    }})()"""
    for got in _run(client, tmp_path, scenario):
        assert got["kind"] == "free" and got["predictor"] is True
        t = _text(got["out"])
        assert "Can't compare yet" in t and "Library-free or predicted (from a FASTA, --predictor" in t
        assert "<small>percentile</small>" not in got["out"]
        assert "--predictor: spectra predicted by deep learning, not the frozen empirical library" in _text(got["msg"])


@needs_node
def test_a_qvalue_005_log_is_refused_truthfully(client, tmp_path):
    rows = _ht60([30000 + 1000 * i for i in range(20)])
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        lkSet('model', 'timsTOF HT'); lkSet('grad', 'evosep:60');
        lkApplyLog(parseDiannLog({json.dumps(_log("hela_timstof_202604.parquet", loaded=53580, qvalue="0.05"))}), false);
        return [lkLog.fdr, els['ws-out'].innerHTML, els['ws-log-out'].innerHTML];
    }})()"""
    fdr, out, msg = _run(client, tmp_path, scenario)
    assert fdr == "other" and "precursor FDR 5%" in _text(msg)
    t = _text(out)
    assert ("Not 1% run-level FDR: STAN counts precursors at Q.Value ≤ 0.01. A report filtered at a looser level, "
            "such as current DIA-NN's 5% default, holds more precursors.") in t
    assert "<small>percentile</small>" not in out and "2.5" not in t


@needs_node
def test_a_dropped_frozen_log_moves_an_unchosen_instrument_to_its_vendor(client, tmp_path):
    """Item 4: the bar's top instrument is the timsTOF HT; STAN's Windows
    Exploris log (frozen Orbitrap library) is placed on the Orbitrap with the
    most runs, and the page says it moved the instrument."""
    rows = (_ht60([30000 + 1000 * i for i in range(20)])
            + [_nano(i, n_precursors=24000 + 200 * i) for i in range(15)]
            + [_row(300 + i, instrument_family="Lumos", instrument_model="Orbitrap Fusion Lumos", spd=32, lc_system="custom",
                    gradient_length_min=44, n_precursors=20000 + i) for i in range(6)])
    text = (LOGS / "stan_pc_2.3.2_exploris_report.log.txt").read_bytes().decode()
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        const before = lk.model;
        lkFile([fakeFile({json.dumps(text)})]);
        return {{ before, model: lk.model, grad: lk.grad, sel: els['ws-model'].value, out: els['ws-out'].innerHTML, msg: els['ws-log-out'].innerHTML }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert got["before"] == "timsTOF HT"
    assert (got["model"], got["grad"], got["sel"]) == ("Orbitrap Exploris 480", "nanolc:38", "Orbitrap Exploris 480")
    assert ("The instrument is now Orbitrap Exploris 480, the Orbitrap with the most runs here, because this is the Orbitrap library. "
            "Change it if you ran another.") in _text(got["msg"])
    t = _text(got["out"])
    assert "47th percentile" in t and "Among the 15 runs in HeLa · Orbitrap Exploris 480" in t
    assert "not the community search" not in t


@needs_node
def test_a_2mb_hostile_log_parses_quickly(client, tmp_path):
    """Item 6: lines over 4,096 characters are never matched and every
    pattern is linear, so 2 MB built to make the old patterns backtrack
    parses in well under 200 ms."""
    scenario = r"""(() => {
        const head = '\nDIA-NN 2.3.0 Academia\n';
        const lines = [];
        const n = 2 * 1024 * 1024;
        // shapes that made the old patterns backtrack, short enough to be read
        const short = ['Output will be filtered at ' + '1'.repeat(4000) + 'x',
                       '[0:00] Loading spectral library ' + ' '.repeat(3960) + 'x' + ' '.repeat(20) + '!',
                       'diann-linux --qvalue ' + '1'.repeat(4000) + 'x --lib ' + ' '.repeat(40),
                       '[0:00] Spectral library loaded: ' + ' and 1'.repeat(600)];
        let size = head.length;
        for (let i = 0; size < n; i++) { const l = short[i % short.length]; lines.push(l); size += l.length + 1; }
        const many = head + lines.join('\n');
        const one = head + '[0:00] Loading spectral library ' + ' '.repeat(n) + 'x';   // one 2 MB line
        const out = {};
        for (const [k, t] of [['many', many], ['one', one]]) {
            const t0 = Date.now(); const o = parseDiannLog(t); out[k] = { ms: Date.now() - t0, ok: o.ok, qvalue: o.qvalue, size: t.length };
        }
        return out;
    })()"""
    got = _run(client, tmp_path, scenario)
    for k in ("many", "one"):
        assert got[k]["size"] >= 2 * 1024 * 1024 and got[k]["ok"] is True, k
        assert got[k]["ms"] < 200, (k, got[k]["ms"])
    assert got["many"]["qvalue"] is None


@needs_node
def test_the_answer_scrolls_into_view_on_submit_and_drop(client, tmp_path):
    """Item 8: at phone width the answer is below the whole form."""
    rows = _ht60([30000 + 1000 * i for i in range(20)])
    text = (LOGS / "stan_hive_2.3.0_timstof_report.log.txt").read_text()
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); renderFilterBar(); renderLookup();
        const scrolled = [];
        document.getElementById('ws-out').scrollIntoView = (o) => scrolled.push(o);
        lkSet('val', '41000');
        const typing = scrolled.length;
        lkSubmit({{ preventDefault() {{}} }});
        lkFile([fakeFile({json.dumps(text)})]);
        return {{ typing, scrolled }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert got["typing"] == 0                                   # typing does not move the page
    assert got["scrolled"] == [{"block": "nearest"}, {"block": "nearest"}]
