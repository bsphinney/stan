"""Community site redesign: the TIC overlay (spec §A.4 items 1-8, §A.3 B5), in the vendored relay.

Spec: docs/superpowers/specs/2026-09-29-community-redesign-and-precursor-lookup-design.md
(§A.4, §A.3 B5); reference implementation: summarise(), lc_class(), grad_label()
and abucket() in docs/community-redesign/mockup/build_mockup.py and the TIC
panel of the mockup template (v3.1, approved 2026-09-29).

Server side: SPACE_VERSION 1.6.0; /api/tic-summary (one summary per QC
standard x mode x SPD x LC cohort, built once per data refresh) and
/api/tic-traces (one cohort's runs, for "show all traces"); /api/tic-overlay
kept; percentiles taken at the same minute, checked against a hand
computation; bands only from 5 runs; the B2 LC rule; no file name, lab name or
submission id in either response; the Python port of the page's read-time
rules gives the same kept rows and cohorts as the page's own JavaScript, on
edge-case rows and on the 2026-09-29 snapshot when it is on this machine.

Page side, run in node against the page's own script (the harness of
tests/test_relay_community_p2b.py, plus a fetch that records every call and
answers the ones a test sets up): the panel opens on its largest cohort, the
menus name gradients as the rest of the page does, the filter bar drives the
QC standard and DIA / DDA, the DDA empty state, "show all traces" loading one
cohort only, escaping of hostile names, and the new TIC code pinned by hash.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import statistics
import subprocess
import sys
from pathlib import Path

import polars as pl
import pytest

from tests.test_relay_community_p1 import SECRET_NAME, SECRET_PRINT, _benchmark_parquet, _main_script, _row
from tests.test_relay_peg import APP_PATH, NODE, _page, client, hub, needs_node, relay  # noqa: F401  (fixtures)

EVIL = '<img src=x onerror="alert(1)">'
SNAP = Path.home() / "stan-handoff-2026-09-29" / "sitereview"
needs_snapshot = pytest.mark.skipif(not (SNAP / "api_tic_overlay.json").exists(),
                                    reason="the 2026-09-29 snapshot is kept outside git")

# ── harness ──────────────────────────────────────────────────────────

_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = fs.readFileSync(process.argv[3], 'utf8');
const data = process.argv[4] ? fs.readFileSync(process.argv[4], 'utf8') : '';
class ClassList {
    constructor() { this.s = new Set(); }
    add(...c) { c.forEach(x => this.s.add(x)); }
    remove(...c) { c.forEach(x => this.s.delete(x)); }
    contains(c) { return this.s.has(c); }
    toggle(c, f) { const on = f === undefined ? !this.s.has(c) : !!f; if (on) this.s.add(c); else this.s.delete(c); return on; }
}
class El {
    constructor(id) {
        this.id = id; this.style = {}; this.textContent = ''; this.value = ''; this.dataset = {}; this.title = '';
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
    addEventListener() {} appendChild() {} scrollIntoView() {}
    querySelector() { return null; } querySelectorAll() { return []; }
}
const els = {};
const byId = (id) => (els[id] = els[id] || new El(id));
const plots = [];
const qsa = {};
const errors = [];
const con = { log() {}, info() {}, warn() {}, error: (...a) => errors.push(a.map(x => (x && x.stack) || String(x)).join(' ')) };
const root = { style: { props: {}, setProperty(k, v) { this.props[k] = v; } } };
const blocked = () => { throw new Error('SecurityError: browser storage is blocked here'); };
// Every fetch is recorded. One whose URL starts with a key of `replies` is
// answered with that body (a number answers with that HTTP status); any
// other never settles, as on a slow network.
const net = [];
const replies = {};
function fetchMock(url) {
    net.push(String(url));
    const key = Object.keys(replies).filter(k => String(url).startsWith(k)).sort((a, b) => b.length - a.length)[0];
    if (key === undefined) return new Promise(() => {});
    const body = replies[key];
    if (typeof body === 'number') return Promise.resolve({ ok: false, status: body, json: () => Promise.resolve({}) });
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(body))) });
}
const realSetTimeout = setTimeout;
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => realSetTimeout(r, 0)); };
const win = { addEventListener() {}, innerWidth: 1280 };
['localStorage', 'sessionStorage'].forEach(k => Object.defineProperty(win, k, { get: blocked }));
const ctx = vm.createContext({
    console: con, els, plots, qsa, root, El, net, replies, flush,
    document: { getElementById: byId, querySelector: () => null, querySelectorAll: (s) => qsa[s] || [],
                addEventListener() {}, createElement: () => new El(''), body: new El('body'), documentElement: root },
    window: win, fetch: fetchMock,
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    Plotly: { newPlot: (id, traces, layout) => plots.push({ id: typeof id === 'string' ? id : id.id, traces, layout }),
              Plots: { resize() {} }, purge() {} },
});
['localStorage', 'sessionStorage'].forEach(k => Object.defineProperty(ctx, k, { get: blocked }));
vm.runInContext(src, ctx);
if (data) vm.runInContext(data, ctx);
Promise.resolve(vm.runInContext(scenario, ctx)).then(
    (out) => { process.stdout.write(JSON.stringify({ out, errors, net }), () => process.exit(0)); },
    (e) => { process.stdout.write(JSON.stringify({ out: null, errors: errors.concat([String((e && e.stack) || e)]), net }), () => process.exit(0)); });
"""


def _run(client, tmp_path: Path, scenario: str, data: str = "", allow_errors: tuple[str, ...] = ()):
    html = _page(client)
    esc_block = re.search(r'<script id="stan-esc">(.*?)</script>', html, re.S).group(1)
    (tmp_path / "main.js").write_text(esc_block + "\n" + _main_script(html))
    (tmp_path / "scenario.js").write_text(scenario)
    (tmp_path / "harness.js").write_text(_HARNESS)
    args = [NODE, str(tmp_path / "harness.js"), str(tmp_path / "main.js"), str(tmp_path / "scenario.js")]
    if data:
        (tmp_path / "data.js").write_text(data)
        args.append(str(tmp_path / "data.js"))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    got = json.loads(proc.stdout)
    errors = [e for e in got["errors"] if not e.startswith(allow_errors)] if allow_errors else got["errors"]
    assert errors == [], errors
    return got


# ── rows ─────────────────────────────────────────────────────────────

def _trace(start: float, n: int = 128, step: float = 0.25, shape: int = 3) -> tuple[list[float], list[float]]:
    """A trace starting at `start` min. A raw MS1 trace starts within 0.1 min
    of acquisition start (its first bin minus half a bin); start later for an
    identified-ion one."""
    rt = [round(start + step * j, 4) for j in range(n)]
    y = [float(1 + (j * shape) % 17 + (j if j < n // 2 else n - j)) for j in range(n)]
    return rt, y


def _tic_row(i: int, start: float = 0.05, shape: int = 3, **over) -> dict:
    rt, y = _trace(start, shape=shape)
    # every row carries each column, as in the stored table
    kw = {"stan_version": "1.2.14", "submitted_at": f"2026-09-29T00:00:{i % 60:02d}.000000Z",
          "tic_rt_bins": json.dumps(rt), "tic_intensity": json.dumps(y)}
    kw.update(over)
    return _row(i, **kw)


def _nano(i: int, **over) -> dict:
    base = dict(instrument_family="Exploris", instrument_model="Orbitrap Exploris 480", spd=38, lc_system="custom",
                gradient_length_min=44, cohort_id="Exploris_30spd_low", n_precursors=22000 + 10 * i)
    base.update(over)
    return _tic_row(i, **base)


def _serve(hub, rows: list[dict]) -> None:
    hub.files["benchmark_latest.parquet"] = _benchmark_parquet(rows)


def _summary(client) -> dict:
    r = client.get("/api/tic-summary")
    assert r.status_code == 200, r.text
    return r.json()


def _entry(summary: dict, s: str, t: str, spd: int, lc: str) -> dict:
    [e] = [c for c in summary["cohorts"] if (c["s"], c["t"], c["spd"], c["lc"]) == (s, t, spd, lc)]
    return e


def _mixed_rows() -> list[dict]:
    """HeLa DIA: 6 Evosep 100 SPD, 3 Evosep 60 SPD + 1 identified-ion,
    2 at 7 SPD nanoLC, 5 nanoLC at 38 SPD (Exploris), 2 with no LC at 60 SPD,
    1 Evosep at 36 SPD (not a method); K562: 2 at 60 SPD."""
    rows = [_tic_row(i) for i in range(6)]
    rows += [_tic_row(10 + i, spd=60, gradient_length_min=21, n_precursors=45000 + i) for i in range(3)]
    rows += [_tic_row(20, spd=60, gradient_length_min=22, start=2.0, stan_version="0.2.282", n_precursors=45900)]
    rows += [_nano(30 + i, spd=7, gradient_length_min=164, instrument_model="Orbitrap Fusion Lumos",
                   instrument_family="Lumos") for i in range(2)]
    rows += [_nano(40 + i) for i in range(5)]
    rows += [_tic_row(50 + i, spd=60, lc_system="", gradient_length_min=21, n_precursors=47000 + i) for i in range(2)]
    rows += [_tic_row(60, spd=36, gradient_length_min=44, n_precursors=48000)]
    rows += [_tic_row(70 + i, sample_type="k562", spd=60, gradient_length_min=21, n_precursors=30000 + i) for i in range(2)]
    return rows


def _loaded(summary: dict) -> str:
    return f"ticSummary = {json.dumps(summary)}; ticStatus = 'ready';"


def _panel() -> str:
    """The panel as a reader sees it, for a scenario to return."""
    return """({ badge: els['tic-badge'].textContent, sel: els['tic-spd-select'].value, opts: els['tic-spd-select'].innerHTML,
              selOff: els['tic-spd-select'].disabled, lcOff: els['tic-lc-select'].disabled,
              allOff: els['tic-show-all'].disabled, allOn: els['tic-show-all'].checked,
              take: els['tic-count'].innerHTML, note: els['tic-note'].textContent, box: els['chart-community-tic'].innerHTML,
              names: ((plots.filter(p => p.id === 'chart-community-tic').slice(-1)[0] || { traces: [] }).traces).map(t => t.name),
              traces: ((plots.filter(p => p.id === 'chart-community-tic').slice(-1)[0] || { traces: [] }).traces).map(t => ({
                  name: t.name, visible: t.visible === undefined ? true : t.visible, dash: (t.line || {}).dash || '',
                  n: (t.x || []).length, hov: t.hovertemplate || '', fill: t.fill || '' })) })"""


# ── server ───────────────────────────────────────────────────────────

def test_space_version_is_1_6_0_or_later(client, relay):
    # The TIC overlay shipped as 1.6.0; P3a (schema) as 1.7.0; P3b as 1.8.0;
    # P3c (facilities) is 1.9.0.
    assert relay.SPACE_VERSION == "1.9.0"
    assert client.get("/api/version").json()["version"] == "1.9.0"
    assert "community site v1.9.0" in _page(client)


def test_summary_shape(client, hub):
    _serve(hub, _mixed_rows())
    s = _summary(client)
    assert set(s) == {"built_at", "rows", "duplicate_copies", "usable", "traces", "not_drawn", "models", "cohorts"}
    assert s["rows"] == 22 and s["usable"] == 22 and s["duplicate_copies"] == 0
    assert s["traces"] == {"DIA": 22, "DDA": 0} and s["not_drawn"] == {"no_spd": 0, "unreadable": 0}
    keys = [(c["s"], c["t"], c["spd"], c["lc"]) for c in s["cohorts"]]
    assert keys == [("hela", "DIA", 7, "nanolc"), ("hela", "DIA", 36, "evosep_unv"), ("hela", "DIA", 38, "nanolc"),
                    ("hela", "DIA", 60, "evosep"), ("hela", "DIA", 60, "unrec"), ("hela", "DIA", 60, "all"),
                    ("hela", "DIA", 100, "evosep"), ("k562", "DIA", 60, "evosep")]
    big = _entry(s, "hela", "DIA", 100, "evosep")
    assert set(big) == {"s", "t", "spd", "lc", "parts", "n", "nid", "labs", "inst", "iinst", "iver", "rt", "b", "solo", "idt"}
    assert big["n"] == 6 and big["nid"] == 0 and big["labs"] == 1 and big["inst"] == [["timsTOF HT", 6]]
    assert big["parts"] == [["evosep", 6, 0, [11, 11]]]           # the stored run length, not the trace end
    assert len(big["rt"]) == 128 and set(big["b"]) == {"p10", "p25", "p50", "p75", "p90"}
    assert all(len(v) == 128 for v in big["b"].values())
    assert big["solo"] == [] and big["idt"] == []
    # every band is ordered, per mille of each run's own peak
    for j in range(128):
        col = [big["b"][k][j] for k in ("p10", "p25", "p50", "p75", "p90")]
        assert col == [None] * 5 or (all(0 <= v <= 1000 for v in col) and col == sorted(col))
    # the "all LC" entry at 60 SPD says what it mixes, the identified-ion trace kept apart
    mix = _entry(s, "hela", "DIA", 60, "all")
    assert mix["parts"] == [["evosep", 3, 1, [21, 22]], ["unrec", 2, 0, [21, 21]]]
    assert mix["n"] == 5 and mix["nid"] == 1 and mix["iver"] == ["0.2.282"] and mix["b"] is not None
    assert len(mix["idt"]) == 1 and mix["idt"][0][1] == 2.0
    # under 5 runs: no bands, each run on its own axis
    seven = _entry(s, "hela", "DIA", 7, "nanolc")
    assert seven["b"] is None and seven["rt"] is None and len(seven["solo"]) == 2
    m, a, b, ys = seven["solo"][0]
    assert s["models"][m] == "Orbitrap Fusion Lumos" and (a, b) == (0.05, 31.8) and len(ys) == 128 and max(ys) == 1000


def test_summary_is_built_once_per_data_refresh(client, hub, relay, monkeypatch):
    """§A.4 item 2: computed once per data refresh and cached, never per request."""
    _serve(hub, _mixed_rows())
    calls = []
    real = relay._tic_build
    monkeypatch.setattr(relay, "_tic_build", lambda df: calls.append(df.height) or real(df))
    first = client.get("/api/tic-summary").content
    for _ in range(3):
        assert client.get("/api/tic-summary").content == first
    assert client.get("/api/tic-traces", params=dict(sample="hela", mode="DIA", spd=100, lc="evosep")).status_code == 200
    assert calls == [22]
    # the submissions cache refills with the same rows: nothing to rebuild
    relay._SUBMISSIONS_CACHE["ts"] = 0.0
    assert client.get("/api/tic-summary").content == first and calls == [22]
    # new rows: rebuilt once, then served from memory again
    _serve(hub, _mixed_rows() + [_tic_row(90 + i, n_precursors=60000 + i) for i in range(2)])
    relay._invalidate_submissions_cache()
    s = client.get("/api/tic-summary").json()
    client.get("/api/tic-summary")
    assert calls == [22, 24] and _entry(s, "hela", "DIA", 100, "evosep")["n"] == 8


def test_traces_endpoint_serves_one_cohort(client, hub):
    _serve(hub, _mixed_rows())
    r = client.get("/api/tic-traces", params=dict(sample="hela", mode="dia", spd=60, lc="all"))
    assert r.status_code == 200
    t = r.json()
    assert set(t) == {"s", "t", "spd", "lc", "n", "models", "raw"}
    assert (t["s"], t["t"], t["spd"], t["lc"], t["n"]) == ("hela", "DIA", 60, "all", 5)
    assert all(len(x) == 4 and len(x[3]) == 128 and max(x[3]) == 1000 for x in t["raw"])   # identified-ion kept out
    assert client.get("/api/tic-traces", params=dict(sample="hela", mode="DIA", spd=61, lc="evosep")).status_code == 404
    assert client.get("/api/tic-traces", params=dict(sample="hela", mode="DDA", spd=100, lc="evosep")).status_code == 404
    # the old endpoint still serves every stored trace, unchanged
    old = client.get("/api/tic-overlay").json()
    assert old["count"] == 22 and set(old["traces"][0]) == {"submission_id", "tic_rt_bins", "tic_intensity"}


def test_an_outage_is_a_503_not_an_empty_benchmark(client, hub):
    """No benchmark table (the dataset could not be read and there is no
    earlier copy): the page must not be told that no traces exist."""
    hub.unreachable.add("benchmark_latest.parquet")
    for path, params in (("/api/tic-summary", {}), ("/api/tic-traces", dict(sample="hela", mode="DIA", spd=100, lc="evosep"))):
        r = client.get(path, params=params)
        assert r.status_code == 503, path
        assert r.json()["unavailable"] is True and r.json()["error"]


def test_no_file_lab_or_submission_names_in_tic_responses(client, hub):
    rows = _mixed_rows()
    for r in rows:
        r.update(display_name="Secret Lab Name", fingerprint=SECRET_PRINT)
    _serve(hub, rows)
    s = client.get("/api/tic-summary")
    bodies = [s.text] + [client.get("/api/tic-traces", params=dict(sample=c["s"], mode=c["t"], spd=c["spd"], lc=c["lc"])).text
                         for c in s.json()["cohorts"]]
    for body in bodies:
        for needle in (SECRET_NAME, SECRET_PRINT, "Secret Lab Name", '"s1"', '"s10"', "submission_id", "run_name", "fingerprint"):
            assert needle not in body, needle


def _hand_bands(traces: list[tuple[list[float], list[float]]]) -> tuple[list[float], dict[str, list]]:
    """An independent computation: percentiles of the peak-scaled traces at
    the same minute of the cohort's median time axis."""
    def at(rt, y, x):
        if x < rt[0] or x > rt[-1]:
            return None
        for i in range(1, len(rt)):
            if rt[i] >= x:
                return y[i - 1] + (y[i] - y[i - 1]) * (x - rt[i - 1]) / (rt[i] - rt[i - 1])
        return y[0]

    def q(v, p):
        x = (len(v) - 1) * p
        lo = int(x)
        return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (x - lo)

    scaled = [(rt, [v / max(y) for v in y]) for rt, y in traces]
    axis = [statistics.median(rt[j] for rt, _ in scaled) for j in range(128)]
    need = max(5, math.ceil(len(traces) / 2))
    out: dict[str, list] = {k: [] for k in ("p10", "p25", "p50", "p75", "p90")}
    for x in axis:
        col = sorted(v for v in (at(rt, y, x) for rt, y in scaled) if v is not None)
        for k, p in (("p10", .1), ("p25", .25), ("p50", .5), ("p75", .75), ("p90", .9)):
            out[k].append(round(1000 * q(col, p)) if len(col) >= need else None)
    return axis, out


def test_percentiles_are_taken_at_the_same_minute(client, hub):
    """§A.4 item 3, against a hand computation. Six runs whose bins sit at
    different minutes (starts 0 to 0.09 min, 0.25 min bins) and differ in
    shape: each is interpolated onto the cohort's median axis, and a minute
    counts only where 5 runs cover it."""
    starts, shapes = [0.0, 0.01, 0.02, 0.03, 0.05, 0.09], [3, 5, 7, 2, 11, 4]
    rows = [_tic_row(i, start=st, shape=sh) for i, (st, sh) in enumerate(zip(starts, shapes))]
    _serve(hub, rows)
    e = _entry(_summary(client), "hela", "DIA", 100, "evosep")
    axis, want = _hand_bands([_trace(st, shape=sh) for st, sh in zip(starts, shapes)])
    assert e["rt"] == [round(x, 2) for x in axis]
    assert e["b"] == want
    # the first minute of the axis (0.025) is covered by 3 runs only: no bands there
    assert e["b"]["p50"][0] is None and e["b"]["p50"][1] is not None
    # the bin-index method the live page used gives other numbers
    scaled = [[v / max(y) for v in y] for _, y in (_trace(st, shape=sh) for st, sh in zip(starts, shapes))]
    by_bin = [round(1000 * statistics.median(sorted(y[j] for y in scaled))) for j in range(128)]
    assert sum(1 for a, b in zip(e["b"]["p50"][1:], by_bin[1:]) if a != b) > 20


def test_bands_only_from_five_runs(client, hub, relay):
    """§A.4 item 4: 4 raw runs are drawn one by one; the 5th brings bands.
    An identified-ion trace never counts toward the five."""
    four = [_tic_row(i) for i in range(4)] + [_tic_row(9, start=2.0, stan_version="0.2.283")]
    _serve(hub, four)
    e = _entry(_summary(client), "hela", "DIA", 100, "evosep")
    assert (e["n"], e["nid"], e["b"], e["rt"], len(e["solo"]), len(e["idt"])) == (4, 1, None, None, 4, 1)
    _serve(hub, four + [_tic_row(5)])
    relay._invalidate_submissions_cache()
    e = _entry(_summary(client), "hela", "DIA", 100, "evosep")
    assert (e["n"], e["nid"], len(e["solo"]), len(e["idt"])) == (5, 1, 0, 1)
    assert len(e["rt"]) == 128 and e["b"]["p50"][10] is not None


def test_lc_class_grouping_matches_the_page_rule(client, hub):
    """§A.4 item 5: Evosep only at an Evosep method; an Evosep run at 36 SPD
    is 'SPD unverified'; a run with no LC at an Evosep-method SPD is 'LC not
    recorded' and never Evosep; no LC at another SPD is nanoLC (lcClass)."""
    rows = _mixed_rows() + [_tic_row(80, spd=38, lc_system="", gradient_length_min=44, instrument_model="Orbitrap Exploris 480",
                                     instrument_family="Exploris", n_precursors=22999)]
    _serve(hub, rows)
    s = _summary(client)
    lcs = {(c["spd"], c["lc"]): c["n"] + c["nid"] for c in s["cohorts"] if c["s"] == "hela"}
    assert lcs[(60, "evosep")] == 4 and lcs[(60, "unrec")] == 2 and lcs[(36, "evosep_unv")] == 1
    assert lcs[(38, "nanolc")] == 6                     # 5 recorded custom + 1 with no LC at a non-Evosep SPD
    assert (60, "nanolc") not in lcs and (38, "unrec") not in lcs


# ── the Python port against the page's JavaScript ────────────────────

_PARITY_JS = r"""(() => {
    const d = dedupeRuns(ROWS);
    const usable = d.kept.filter(s => !s.is_flagged && !isHeldBack(s));
    const groups = {}, rowsOf = {};
    for (const s of usable) {
        if (!(s.tic_rt_bins && s.tic_intensity)) continue;
        let rt, y;
        try { rt = JSON.parse(s.tic_rt_bins); y = JSON.parse(s.tic_intensity); } catch (e) { continue; }
        if (!Array.isArray(rt) || !Array.isArray(y) || rt.length < 2 || rt.length !== y.length) continue;
        const lc = lcClass(s);
        if (lc === 'nospd') continue;
        const k = [s.sample_type || 'hela', trackOf(s), spdOf(s), lc].join('|');
        (groups[k] = groups[k] || []).push([s.submission_id, rt[0] - (rt[1] - rt[0]) / 2 > 0.1]);
        (rowsOf[k] = rowsOf[k] || []).push(s);
    }
    const meta = {};
    for (const [k, rs] of Object.entries(rowsOf)) {
        const raw = rs.filter((s, i) => !groups[k][i][1]), idt = rs.filter((s, i) => groups[k][i][1]);
        meta[k] = { labs: labCount(raw.length ? raw : idt), len: runLenText(rs) };
    }
    return { kept: d.kept.map(s => s.submission_id), dropped: d.dropped, usable: usable.map(s => s.submission_id), groups, meta };
})()"""


def _py_parity(relay) -> dict:
    g = relay._tic_groups(relay._load_all_submissions())
    groups = {}
    for (s, t, spd), by_lc in g["groups"].items():
        for lc, ts in by_lc.items():
            groups[f"{s}|{t}|{spd}|{lc}"] = sorted([x["row"]["submission_id"], x["idion"]] for x in ts)
    return {"kept": [r["submission_id"] for r in g["kept"]], "dropped": g["dropped"],
            "usable": [r["submission_id"] for r in g["usable"]], "groups": groups}


def _js_parity(client, tmp_path, rows: list[dict]) -> dict:
    got = _run(client, tmp_path, _PARITY_JS, data=f"var ROWS = {json.dumps(rows)};")["out"]
    got["groups"] = {k: sorted(v) for k, v in got["groups"].items()}
    return got


def _page_rows(client) -> list[dict]:
    """What the page holds: /api/leaderboard's rows, in its order, with the
    traces merged in by submission id as the page used to."""
    rows = client.get("/api/leaderboard").json()["submissions"]
    tic = {t["submission_id"]: t for t in client.get("/api/tic-overlay").json()["traces"]}
    for r in rows:
        t = tic.get(r["submission_id"])
        if t:
            r["tic_rt_bins"], r["tic_intensity"] = t["tic_rt_bins"], t["tic_intensity"]
    return rows


def _assert_parity(client, relay, tmp_path) -> tuple[dict, dict]:
    js, py = _js_parity(client, tmp_path, _page_rows(client)), _py_parity(relay)
    assert py["kept"] == js["kept"]
    assert py["dropped"] == js["dropped"]
    assert py["usable"] == js["usable"]
    assert py["groups"] == js["groups"]
    # the summary's counts, labs and run lengths are the page's
    s = _summary(client)
    for k, m in js["meta"].items():
        sm, t, spd, lc = k.split("|")
        e = _entry(s, sm, t, int(spd), lc)
        assert [e["n"], e["nid"]] == [sum(1 for _, i in js["groups"][k] if not i), sum(1 for _, i in js["groups"][k] if i)], k
        assert e["labs"] == m["labs"], k
        lo, hi = e["parts"][0][3] or (None, None)
        assert m["len"] == ("" if lo is None else f"{lo} min run" if lo == hi else f"{lo}–{hi} min runs"), k
    return js, py


@needs_node
def test_python_port_matches_the_page_on_edge_cases(client, hub, relay, tmp_path):
    """Copies, held-back and flagged rows, and the value shapes a parquet
    column can hand the page (counts and SPD as floats, every run_date form,
    a half SPD, LC with spaces and capitals, no sample type or lab name,
    'Anonymous Lab', a trace that is not JSON)."""
    base = dict(n_precursors=41000.0, n_peptides=36000, n_proteins=5000, n_psms=0)
    rows = [
        _tic_row(1, run_date="2026-09-20T10:00:00.123456+00:00", submitted_at="2026-09-21T00:00:00.000001Z", **base),
        _tic_row(2, run_date="2026-09-20T03:00:01-07:00", submitted_at="2026-09-21T00:00:00.000002Z",
                 display_name="Anonymous Lab", **base),                                   # a copy, 1 s later
        _tic_row(3, run_date="2026-09-20T10:00:02.900Z", submitted_at="2026-09-20T00:00:00.000000Z",
                 amount_ng=50000, **base),                                                # held back: never kept
        _tic_row(4, run_date="2026-09-20T10:00:05Z", **base),                             # 5 s on: its own run
        _tic_row(5, run_date="2026-09-20T10:00:05.5Z", is_flagged=True, **base),          # flagged copy of 4
        _tic_row(6, run_date="2026-09-21", spd=59.5, lc_system=" Evosep ", sample_type=None),
        _tic_row(7, run_date="not a date", amount_ng=60.0),
        _tic_row(8, run_date=None, lc_system="", spd=60, display_name=None),
        _tic_row(9, run_date="2026-09-22T10:00:00Z", n_precursors=40500, spd=0),           # no SPD: not drawn
        _tic_row(10, run_date="2026-09-22T11:00:00+05:30", spd=36, start=2.0),             # identified-ion
        _tic_row(11, run_date="2026-09-22T12:00:00Z", tic_rt_bins="not json"),
        # offsets without their colon, as V8 reads them: one acquisition
        _tic_row(12, run_date="2026-09-23T10:00:00+0000", **dict(base, n_precursors=42000.0)),
        _tic_row(13, run_date="2026-09-23T03:00:01-0700", **dict(base, n_precursors=42000.0)),
        # a day past the month's end rolls over in V8 (2 March): one acquisition
        _tic_row(14, run_date="2026-02-30T10:00:00Z", **dict(base, n_precursors=43000.0)),
        _tic_row(15, run_date="2026-03-02T10:00:01Z", **dict(base, n_precursors=43000.0)),
        # 24:00 is the next midnight, and a lower-case z is UTC: one acquisition
        _tic_row(16, run_date="2026-09-20T24:00:00Z", **dict(base, n_precursors=44000.0)),
        _tic_row(17, run_date="2026-09-21t00:00:01z", **dict(base, n_precursors=44000.0)),
        # month 13 is NaN to V8: undated, never a copy, both kept
        _tic_row(20, run_date="2026-13-01T10:00:00Z", **dict(base, n_precursors=45000.0)),
        _tic_row(21, run_date="2026-13-01T10:00:00Z", **dict(base, n_precursors=45000.0)),
        # trim() drops a BOM but keeps U+001C, which Python's strip() would drop
        _tic_row(18, run_date="2026-09-24T10:00:00Z", lc_system="\ufeffEvosep\ufeff"),
        _tic_row(19, run_date="2026-09-24T11:00:00Z", lc_system="\x1cEvosep"),
    ]
    _serve(hub, rows)
    js, py = _assert_parity(client, relay, tmp_path)
    assert "s3" not in py["kept"] and "s2" not in py["kept"] and "s1" in py["kept"]
    assert "s4" in py["kept"] and "s5" not in py["kept"]
    assert ("s12" in py["kept"]) != ("s13" in py["kept"])
    assert ("s14" in py["kept"]) != ("s15" in py["kept"])
    assert ("s16" in py["kept"]) != ("s17" in py["kept"])
    assert "s20" in py["kept"] and "s21" in py["kept"] and py["dropped"] == 6
    assert ["s18", False] in py["groups"]["hela|DIA|100|evosep"]
    assert py["groups"]["hela|DIA|100|nanolc"] == [["s19", False]]
    assert py["groups"]["hela|DIA|60|evosep"] == [["s6", False]]
    assert py["groups"]["hela|DIA|60|unrec"] == [["s8", False]]
    assert ["s10", True] in py["groups"]["hela|DIA|36|evosep_unv"]
    assert not any("s9" in str(v) or "s11" in str(v) for v in py["groups"].values())


# Every date-time form V8's Date.parse reads as ISO 8601, and the odd ones:
# offsets with and without a colon, rolled-over days, 24:00, lower-case t/z,
# a space for T, short dates, signed 6-digit years, the time-value limits,
# and what V8 refuses. Offset-less times are left out: local to a browser,
# UTC on the relay, and STAN always writes an offset.
RUN_DATES = [
    "2026-09-20T10:00:00+0700", "2026-09-20T10:00:00.123+0700", "2026-09-20T10:00:00-0730", "2026-09-20T10:00:00+07",
    "2026-09-20T10:00:00 +00:00", "2026-02-30", "2026-02-30T10:00:00Z", "2026-02-29T10:00:00Z",
    "2024-02-29T10:00:00Z", "2024-02-30T10:00:00Z", "2026-04-31T10:00:00+00:00", "2026-13-01T10:00:00Z",
    "2026-00-10T10:00:00Z", "2026-09-00T10:00:00Z", "2026-09-32T10:00:00Z", "2026-09-20T24:00:00Z",
    "2026-09-20T24:00:00.000Z", "2026-09-20T24:00Z", "2026-09-20T24:00:01Z", "2026-09-20T24:00:00.001Z",
    "2026-09-20T25:00:00Z", "2026-09-20T10:60:00Z", "2026-09-20T10:00:60Z", "2026-09-20t10:00:00z",
    "2026-09-20T10:00Z", "2026-09-20 10:00:00+00:00", "2026-09-20 10:00:00Z", "2026-09-20T10:00:00.5Z",
    "2026-09-20T10:00:00.12Z", "2026-09-20T10:00:00.123456Z", "2026-09-20T10:00:00.123456+05:30", "2026-09-20T10Z",
    "20260920", "2026-09-20T10:00:00+24:00", "2026-09-20T10:00:00+23:59", "2026-09-20T10:00:00+07:60",
    "2026-09-20T10:00:00.Z", "2026-09-20T10:00:00,123Z", "+002026-09-20T10:00:00Z", "+002026-02-30T10:00:00Z",
    "2026-09-20T10:00:00UTC", "2026-09-20T10:00:00 GMT", "2026-9-20T10:00:00Z", "2026-09",
    "2026", "2026Z", "2026-09-20Z", "2026-09-20+07:00",
    "2026T10:00Z", "2026-09T10:00Z", " 2026-09-20T10:00:00Z", "2026-09-20T10:00:00Z ",
    "2026-09-20T10:00:00+0", "2026-09-20T10:00:00+070", "2026-09-20T10:00:00+07000", "-000001-01-01T00:00:00Z",
    "0000-01-01T00:00:00Z", "-000000-01-01T00:00:00Z", "+275760-09-13T00:00:00Z", "+275760-09-13T00:00:00.001Z",
    "-271821-04-20T00:00:00Z", "-271821-04-19T23:59:59.999Z", "2026-09-20T23:59:59.999+23:59", "2026-09-20T00:00:00-23:59",
    "2026-09-20T10:00:00+07:00Z", "1900-02-29T00:00:00Z", "2000-02-29T00:00:00Z", "2100-02-29T00:00:00Z",
    "1969-12-31T23:59:59.999Z", "not a date", ""
]
# Characters at either end of an LC name: JS trim() and the port must agree.
LC_ENDS = ["\t", "\n", "\v", "\f", "\r", " ", "\x1c", "\x1f", "\x85", "\xa0", "\u1680", "\u180e", "\u2000",
           "\u200a", "\u200b", "\u2028", "\u2029", "\u202f", "\u205f", "\u3000", "\ufeff"]


@needs_node
def test_dates_and_lc_names_are_read_as_the_page_reads_them(client, relay, tmp_path):
    lcs = [c + "Evosep" + c for c in LC_ENDS] + ["\ufeff Evosep \ufeff", "EVOSEP\u3000", "evo sep"]
    scenario = f"""(() => ({{
        dates: {json.dumps(RUN_DATES)}.map(d => {{ const t = _instantMs({{ run_date: d }}); return isFinite(t) ? t : null; }}),
        lcs: {json.dumps(lcs)}.map(lc => lcClass({{ spd: 100, lc_system: lc }})) }}))()"""
    got = _run(client, tmp_path, scenario)["out"]
    assert [relay._page_instant_ms({"run_date": d}) for d in RUN_DATES] == got["dates"]
    assert [relay._page_lc_class({"spd": 100, "lc_system": lc}) for lc in lcs] == got["lcs"]
    # the cases the review named, as V8 reads them
    at = dict(zip(RUN_DATES, got["dates"]))
    assert at["2026-09-20T10:00:00+0700"] == relay._page_instant_ms({"run_date": "2026-09-20T03:00:00Z"})
    assert at["2026-02-30T10:00:00Z"] == relay._page_instant_ms({"run_date": "2026-03-02T10:00:00Z"})
    assert at["2026-13-01T10:00:00Z"] is None and at["2026-09-20T10:00:00+07"] is None
    assert got["lcs"][LC_ENDS.index("\ufeff")] == "evosep" and got["lcs"][LC_ENDS.index("\x1c")] == "nanolc"


@needs_node
@needs_snapshot
def test_python_port_matches_the_page_on_the_2026_09_29_snapshot(client, hub, relay, tmp_path):
    """§A.4 item 5: the summary is built from exactly the rows the page keeps.
    3,305 rows -> 3,061 kept (244 copies) -> 2,990 usable (71 held back).

    Relay 1.6.0 held back 2 (stored amount above 5,000 ng): 3,059 usable,
    3,023 traces, 626 at 100 SPD Evosep. P3b (1.8.0) also holds back the 71
    rows whose file name states another amount: 69 acquisitions as the page
    keeps them (2 rows are copies). The dedupe keeps the copy it kept before
    P3b, so no older seed copy is swapped in. Only the held-back set moved:
    the TIC code and its pins are unchanged."""
    rows = json.loads((SNAP / "api_leaderboard.json").read_text())["submissions"]
    tic = {t["submission_id"]: t for t in json.loads((SNAP / "api_tic_overlay.json").read_text())["traces"]}
    for r in rows:
        t = tic.get(r["submission_id"])
        r["tic_rt_bins"] = t["tic_rt_bins"] if t else None
        r["tic_intensity"] = t["tic_intensity"] if t else None
    buf = io.BytesIO()
    pl.from_dicts(rows, infer_schema_length=None).write_parquet(buf)
    hub.files["benchmark_latest.parquet"] = buf.getvalue()
    js, py = _assert_parity(client, relay, tmp_path)
    assert (len(py["kept"]), py["dropped"], len(py["usable"])) == (3061, 244, 2990)
    assert len(py["groups"]["hela|DIA|100|evosep"]) == 585
    assert sum(len(v) for v in py["groups"].values()) == 2954


# ── page ─────────────────────────────────────────────────────────────

@needs_node
def test_opens_on_the_largest_cohort_with_bands(client, hub, tmp_path):
    _serve(hub, _mixed_rows())
    got = _run(client, tmp_path, f"(() => {{ {_loaded(_summary(client))} renderCommunityTIC(); return {_panel()}; }})()")["out"]
    assert got["sel"] == "100"
    assert "6 runs · 1 lab" in got["take"] and "single-lab reference" in got["take"]
    assert "Evosep 100 SPD: the median at each minute" in got["take"]
    assert "Median (6 runs · 1 lab)" in got["names"] and "25–75th pct (IQR)" in got["names"] and "10–90th pct" in got["names"]
    assert got["note"].startswith("Trace: the MS1 total-ion chromatogram from the raw file, scaled to its own peak.")
    assert "Instruments: timsTOF HT 6." in got["note"] and "each percentile is taken across the runs at the same minute" in got["note"]
    assert not any(x in got["take"] + got["note"] for x in ("Identified (DIA)", "identified-ion trace", "raw (DDA)"))
    assert got["selOff"] is False and got["lcOff"] is False and got["allOff"] is False


@needs_node
def test_menu_labels(client, hub, tmp_path):
    """§A.4 item 6: the stored run length, "(no bands)", All says what it
    mixes, nanoLC named by its gradient as on the rest of the page."""
    _serve(hub, _mixed_rows())
    s = _summary(client)
    scenario = f"""(async () => {{ {_loaded(s)} const out = {{}};
        for (const lc of ['all', 'evosep', 'custom']) {{ document.getElementById('tic-lc-select').value = lc; renderCommunityTIC();
            out[lc] = [...els['tic-spd-select'].innerHTML.matchAll(/<option value="(\\d+)"[^>]*>([^<]*)</g)].map(m => m[2]); }}
        document.getElementById('tic-lc-select').value = 'all'; ticPickSpd('60'); out.take60 = els['tic-count'].innerHTML;
        ticPickSpd('38'); out.take38 = els['tic-count'].innerHTML;
        ticPickSpd('36'); out.take36 = els['tic-count'].innerHTML;
        return out; }})()"""
    got = _run(client, tmp_path, scenario)["out"]
    assert got["all"] == [
        "7 SPD · nanoLC · ~165 min gradient · 164 min run · 2 runs (no bands)",
        "36 SPD · Evosep · 44 min run (SPD unverified) · 1 run (no bands)",
        "38 SPD · nanoLC · ~30 min gradient · 44 min run · 5 runs",
        "60 SPD · Evosep 21–22 min + LC not recorded 21 min · 5 runs +1 identified-ion",
        "100 SPD · Evosep · 11 min run · 6 runs",
    ]
    assert got["evosep"] == ["36 SPD · Evosep · 44 min run (SPD unverified) · 1 run (no bands)",
                             "60 SPD · Evosep · 21–22 min runs · 3 runs +1 identified-ion (no bands)",
                             "100 SPD · Evosep · 11 min run · 6 runs"]
    assert got["custom"] == ["7 SPD · nanoLC · ~165 min gradient · 164 min run · 2 runs (no bands)",
                             "38 SPD · nanoLC · ~30 min gradient · 44 min run · 5 runs"]
    assert "60 SPD, all LC systems" in got["take60"]
    assert "<b>All mixes gradients here:</b> Evosep 21–22 min (3 + 1 identified-ion) + LC not recorded 21 min (2)." in got["take60"]
    assert "~30 min gradient (38 SPD) · 44 min run: the median at each minute" in got["take38"]
    assert "the one run (too few for a median) · 1 lab" in got["take36"]
    assert "Evosep, 44 min run (SPD 36 unverified)" in got["take36"]


@needs_node
def test_below_five_runs_each_run_is_drawn_and_identified_ion_kept_out(client, hub, tmp_path):
    _serve(hub, _mixed_rows())
    scenario = f"""(() => {{ {_loaded(_summary(client))} document.getElementById('tic-lc-select').value = 'evosep'; ticPickSpd('60');
        return {_panel()}; }})()"""
    got = _run(client, tmp_path, scenario)["out"]
    assert "each of 3 runs (too few for a median) · 1 lab" in got["take"]
    names = [t["name"] for t in got["traces"]]
    assert not any(n.startswith("Median") or "pct" in n for n in names)
    assert names.count("timsTOF HT") == 3                         # one per run, one colour per instrument
    [idion] = [t for t in got["traces"] if t["dash"] == "dot"]
    assert idion["name"] == "Identified-ion traces, STAN 0.2.282 (1)" and idion["visible"] == "legendonly"
    assert "1 identified-ion trace from STAN 0.2.282 is kept out of the median" in got["note"]
    assert "Each run is drawn on its own time axis, coloured by instrument." in got["note"]
    assert got["allOff"] is True                                  # every run is already drawn


@needs_node
def test_dda_empty_state_says_it_once_and_turns_controls_off(client, hub, tmp_path):
    """§A.4 item 8, with DIA / DDA from the filter bar."""
    _serve(hub, _mixed_rows())
    scenario = f"""(() => {{ {_loaded(_summary(client))}
        renderCommunityTIC(); document.getElementById('tic-show-all').checked = true; setView({{ mode: 'dda' }});
        const p = {_panel()}; return p; }})()"""
    got = _run(client, tmp_path, scenario)["out"]
    assert got["box"] == '<p style="color:var(--text-muted)">No DDA TIC traces have been submitted yet. All 22 traces in the benchmark are DIA.</p>'
    assert got["take"] == "" and got["note"] == ""
    assert got["selOff"] is True and got["lcOff"] is True and got["allOff"] is True and got["allOn"] is False
    assert got["badge"].startswith("HeLa · DDA")


@needs_node
def test_the_filter_bar_drives_the_tic(client, hub, tmp_path):
    rows = _mixed_rows()
    _serve(hub, rows)
    scenario = f"""(() => {{ {_loaded(_summary(client))} setSubmissions({json.dumps(rows)}); renderFilterBar();
        const n = () => plots.filter(p => p.id === 'chart-community-tic').length, out = {{}};
        renderCommunityTIC(); out.start = [{_panel()}.sel, {_panel()}.badge, n()];
        setView({{ amount: 'all' }}); out.amount = n();                  // the TIC does not follow the amount
        setView({{ sample: 'k562' }}); out.k562 = [{_panel()}.sel, {_panel()}.take, {_panel()}.badge, n()];
        setView({{ sample: 'hela', mode: 'all' }}); out.both = [{_panel()}.sel, {_panel()}.badge];
        setView({{ sample: 'all' }}); out.allStd = [{_panel()}.badge, {_panel()}.take];
        setView({{ sample: 'hela' }});
        setView({{ mode: 'dda' }}); out.dda = {_panel()}.box;
        setView({{ mode: 'dia' }}); out.back = {_panel()}.sel;
        out.follows = panelFollows('community-tic');
        return out; }})()"""
    got = _run(client, tmp_path, scenario)["out"]
    assert got["start"][:2] == ["100", "HeLa · DIA · all amounts"]
    assert got["amount"] == got["start"][2]
    assert got["k562"][0] == "60" and "each of 2 runs (too few for a median)" in got["k562"][1]
    assert got["k562"][2].startswith("K562 · DIA") and got["k562"][3] > got["start"][2]
    # the reader's SPD survives a change of standard or mode when the new view has it
    assert got["both"] == ["60", "HeLa · DIA (DIA and DDA are never averaged together) · all amounts"]
    # "All standards": the standard with the most runs, said in the badge
    assert got["allStd"][0] == "HeLa (one QC standard at a time) · DIA (DIA and DDA are never averaged together) · all amounts"
    assert "60 SPD, all LC systems" in got["allStd"][1]
    assert "No DDA TIC traces have been submitted yet." in got["dda"]
    assert got["back"] == "60" and got["follows"] == ["sample", "mode"]


@needs_node
def test_show_all_traces_loads_that_cohort_only(client, hub, tmp_path):
    """§A.4 item 2: nothing but the summary loads by default; ticking the
    box loads the shown cohort's runs, once, and another cohort's only when
    it is shown."""
    _serve(hub, _mixed_rows())
    s = _summary(client)
    t100 = client.get("/api/tic-traces", params=dict(sample="hela", mode="DIA", spd=100, lc="evosep")).json()
    t38 = client.get("/api/tic-traces", params=dict(sample="hela", mode="DIA", spd=38, lc="nanolc")).json()
    scenario = f"""(async () => {{ {_loaded(s)}
        replies['/api/tic-traces?sample=hela&mode=DIA&spd=100&lc=evosep'] = {json.dumps(t100)};
        replies['/api/tic-traces?sample=hela&mode=DIA&spd=38&lc=nanolc'] = {json.dumps(t38)};
        const out = {{}}; const tic = () => net.filter(u => u.startsWith('/api/tic'));
        renderCommunityTIC(); out.before = tic();
        document.getElementById('tic-show-all').checked = true; ticToggleAll(); out.loading = {_panel()}.note;
        await flush(); out.one = tic(); out.drawn = {_panel()}.traces;
        ticPickSpd('7'); out.seven = tic();                                 // too few for bands: nothing to load
        ticPickSpd('38'); await flush(); out.two = tic(); out.drawn38 = {_panel()}.names;
        ticPickSpd('100'); await flush(); out.three = tic();               // already loaded
        out.note = {_panel()}.note;
        return out; }})()"""
    got = _run(client, tmp_path, scenario)
    out = got["out"]
    assert out["before"] == []
    assert "Loading all 6 runs…" in out["loading"]
    assert out["one"] == ["/api/tic-traces?sample=hela&mode=DIA&spd=100&lc=evosep"]
    runs = [t for t in out["drawn"] if t["name"].endswith("runs (6)")]
    assert [t["name"] for t in runs] == ["timsTOF HT runs (6)"] and runs[0]["n"] == 6 * 129
    assert out["drawn"][-1]["name"] == "Median (6 runs · 1 lab)"            # the bands stay on top
    assert out["seven"] == out["one"]
    assert out["two"] == out["one"] + ["/api/tic-traces?sample=hela&mode=DIA&spd=38&lc=nanolc"]
    assert "Orbitrap Exploris 480 runs (5)" in out["drawn38"]
    assert out["three"] == out["two"]
    assert '"Show all traces" draws every one of the 6 runs' in out["note"]
    assert not any("/api/tic-overlay" in u for u in got["net"])


@needs_node
def test_page_load_fetches_the_summary_not_every_trace(client, hub, tmp_path):
    rows = _mixed_rows()
    _serve(hub, rows)
    board = client.get("/api/leaderboard").json()
    scenario = f"""(async () => {{ replies['/api/leaderboard'] = {json.dumps(board)};
        replies['/api/tic-summary'] = {json.dumps(_summary(client))};
        await loadData(); await flush();
        return {{ net: net.slice(), panel: {_panel()} }}; }})()"""
    got = _run(client, tmp_path, scenario)["out"]
    assert got["net"] == ["/api/leaderboard", "/api/leaderboard", "/api/tic-summary"]   # the page's own load, then this one
    assert got["panel"]["sel"] == "100" and "Median (6 runs · 1 lab)" in got["panel"]["names"]


@needs_node
def test_summary_failure_says_so(client, tmp_path):
    """A 503 from the relay (an outage) is said as one, never as "no traces"."""
    scenario = f"""(async () => {{ replies['/api/tic-summary'] = 503; renderCommunityTIC(); const a = {_panel()}.box;
        await loadTicSummary(); return [a, {_panel()}.box, {_panel()}.selOff, {_panel()}.lcOff]; }})()"""
    loading, failed, off, lc_off = _run(client, tmp_path, scenario)["out"]
    assert "Loading the TIC summaries…" in loading
    assert "The TIC summaries could not be loaded. Reload the page to try again." in failed
    assert "submitted" not in failed and off is True and lc_off is True


@needs_node
def test_a_leaderboard_failure_does_not_leave_the_tic_loading(client, tmp_path):
    scenario = f"""(async () => {{ replies['/api/leaderboard'] = 500; await loadData(); await flush();
        return {{ net: net.slice(), panel: {_panel()} }}; }})()"""
    got = _run(client, tmp_path, scenario, allow_errors=("[loadData] fetch failed",))["out"]
    assert got["net"] == ["/api/leaderboard", "/api/leaderboard"]        # the summaries are not asked for
    assert "The benchmark data did not load, so the TIC summaries were not requested." in got["panel"]["box"]
    assert "Loading" not in got["panel"]["box"]
    assert got["panel"]["selOff"] and got["panel"]["lcOff"] and got["panel"]["allOff"]


@needs_node
def test_the_bars_reset_takes_the_tic_back_to_its_largest_cohort(client, hub, tmp_path):
    rows = _mixed_rows()
    _serve(hub, rows)
    scenario = f"""(async () => {{ {_loaded(_summary(client))} setSubmissions({json.dumps(rows)}); renderFilterBar(); renderCommunityTIC();
        document.getElementById('tic-lc-select').value = 'custom'; ticPickSpd('38');
        document.getElementById('tic-show-all').checked = true; ticToggleAll(); setView({{ amount: 'all' }});
        const before = {_panel()};
        resetView(); ticReset();                          // what the bar's Reset button runs
        return [before, {_panel()}, {{ ...view }}, document.getElementById('tic-lc-select').value]; }})()"""
    before, after, v, lc = _run(client, tmp_path, scenario)["out"]
    assert before["sel"] == "38" and before["allOn"] is True
    assert after["sel"] == "100" and lc == "all" and after["allOn"] is False and v["amount"] == "50"
    assert "626" not in after["take"] and "6 runs · 1 lab" in after["take"]
    html = _page(client)
    assert '<button type="button" class="fbar-btn" id="fbar-reset" onclick="resetView(); ticReset()" hidden>Reset</button>' in html


def test_leaving_full_screen_puts_the_plot_height_back(client):
    """Every chart with the ⛶ button: the height is saved on the way in and
    put back on the way out, from the button and from the browser's exit
    (Esc), with width: null so the width follows the card again (a relayout
    with only the height and autosize off left the full-screen width).
    tic_check.py measures it in Chrome at 1280 and 400 px."""
    main = _main_script(_page(client))
    expand = main[main.index("function _stanFsEnter(pd) {"):main.index("window.addEventListener('load', () => { try { _stanInjectExpand(); }")]
    assert "pd._stanH = (pd.layout && pd.layout.height) || (pd._fullLayout && pd._fullLayout.height) || null;" in expand
    assert "Plotly.relayout(pd, { height: h, width: null })" in expand
    assert expand.count("_stanFsExit(") == 3            # its definition, the Esc path and the button
    assert "_stanFsExit(c.querySelector('[id^=\"chart-\"]'));" in expand and "_stanFsEnter(plot);" in expand
    assert "Plotly.Plots.resize(plot)" not in expand


@needs_node
def test_hostile_names_are_escaped(client, hub, tmp_path):
    """Instrument models and versions come from submitters: escaped in the
    menu, the take line, legend names and hovers. Lab names never reach the panel."""
    rows = [_tic_row(i, instrument_model=EVIL, display_name=EVIL) for i in range(5)]
    rows += [_tic_row(10 + i, spd=60, instrument_model=EVIL, display_name=EVIL, gradient_length_min=21) for i in range(2)]
    rows += [_tic_row(20, spd=60, start=2.0, stan_version=EVIL, instrument_model=EVIL, display_name=EVIL, gradient_length_min=21,
                      n_precursors=1)]
    _serve(hub, rows)
    s = _summary(client)
    t = client.get("/api/tic-traces", params=dict(sample="hela", mode="DIA", spd=100, lc="evosep")).json()
    scenario = f"""(async () => {{ {_loaded(s)} replies['/api/tic-traces'] = {json.dumps(t)};
        renderCommunityTIC(); document.getElementById('tic-show-all').checked = true; ticToggleAll(); await flush();
        const a = {_panel()}; ticPickSpd('60'); const b = {_panel()}; return [a, b]; }})()"""
    a, b = _run(client, tmp_path, scenario)["out"]
    for p in (a, b):
        html = p["take"] + p["opts"] + "".join(x["name"] + x["hov"] for x in p["traces"])
        assert EVIL not in html
        assert "<img" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; runs (5)" in [x["name"] for x in a["traces"]]
    assert any(x["name"] == "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" for x in b["traces"])
    assert any("Identified-ion traces, STAN &lt;img" in x["name"] for x in b["traces"])


# ── page text and pins ───────────────────────────────────────────────

def test_card_markup(client):
    html = _page(client)
    i = html.index('<div class="chart-card chart-full" id="tic-card">')
    card = html[i:html.index('<p class="chart-note" id="tic-note"></p>', i)]
    assert '<h3>Community TIC Overlay by SPD <span class="fbadge" id="tic-badge"></span></h3>' in card
    assert "The MS1 total-ion chromatogram from the raw file" in card
    assert "Identified (DIA)" not in html and "raw (DDA)" not in html
    for ctl in ('<select id="tic-spd-select" class="tic-sel" onchange="ticPickSpd(this.value)" disabled>',
                '<select id="tic-lc-select" class="tic-sel" onchange="renderCommunityTIC()">',
                '<option value="all">All LC systems</option>', '<option value="evosep">Evosep only</option>',
                '<option value="custom">Custom / nanoLC only</option>',
                '<input type="checkbox" id="tic-show-all" onchange="ticToggleAll()"><span>show all traces</span>'):
        assert ctl in card, ctl
    assert 'id="tic-mode-select"' not in html            # DIA / DDA is the bar's now
    # the chart is a direct child of its .chart-card, so the ⛶ button and full screen apply
    assert '<p class="tic-take" id="tic-count" aria-live="polite"></p>\n            <div id="chart-community-tic"></div>' in card
    main = _main_script(html)
    assert "fetch('/api/tic-overlay')" not in main and "loadTicSummary();" in main
    assert "}, { responsive: true });   // the modebar stays: zoom, pan and the PNG download" in main


def _tic_regions(html: str) -> dict[str, str]:
    def between(a: str, b: str, incl_b: bool = False) -> str:
        i = html.index(a)
        j = html.index(b, i + len(a))
        return html[i:j + (len(b) if incl_b else 0)]
    return {
        "tic_js": between("// ── Community TIC Overlay (spec §A.4, relay 1.6.0)", "\n// ── Lab trend vs. reference"),
        "tic_card": between('<div class="chart-card chart-full" id="tic-card">', '<p class="chart-note" id="tic-note"></p>', True),
        "tic_css": between("/* ── Community TIC overlay (relay 1.6.0, spec §A.4) ──", "    </style>"),
    }


# Pinned from this change (relay 1.6.0). A later change to the TIC overlay
# recomputes these with _tic_regions() and says why.
TIC_PINS = {
    "tic_js": "380da8949d87626ed9577c9fd22a498ce8cff8fb910f6a6126140956192b610d",
    "tic_card": "38f19e7f2296e5bd836365697017232fbc630c1b93dae2aa23cc581fa16d47f7",
    "tic_css": "18a1978daa0909a01c7f957d55d014cf3d7270503f71fe4a7401384d5d8c9e88",
}


def test_tic_code_is_pinned(client):
    got = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in _tic_regions(_page(client)).items()}
    assert got == TIC_PINS


# Pinned from main ede086b (relay 1.5.0): the lookup ("Where does my run
# sit?": its script, section and CSS) and PEG Watch's server code are not
# touched by the TIC change. PEG Watch's page parts and the P2a/P2b read-time
# rules are pinned in tests/test_relay_community_p2b.py and _p2c.py.
# P3b (relay 1.8.0) changed the lookup on purpose, by one line of script and
# one line of text: it has no FAIMS field, so it compares a run with runs
# acquired without FAIMS, and says so. The test takes both back out before
# hashing, so the pins stay ede086b's.
UNCHANGED_SINCE_EDE086B = {
    "lookup_js": "c8679618e6ab8fadb8d8066318ad8da410c2330921fcd8842b5f569f25021a3c",
    "lookup_html": "d83ece0137c35bd9aa1a84e50ffd26351cfb2b25a07d2e94bbca32a391016ea1",
    "lookup_css": "7ea77595042d0499baf9edaf450c36a175856caf9640d845b7a7f3687f2d454d",
    "peg_server": "1c2e87a9717fd79678afd859ffd165163f91f7cbbe40f13590349fabcbda6386",
}


# (P3b text, ede086b text)
LOOKUP_P3B = (
    "// The lookup has no FAIMS field (FAIMS is in the cohort key and titles only,\n"
    "// decision 9), so it compares a run with runs acquired without FAIMS (P3b).\n"
    "function lkRows() { return rowsOfSample(lkSample()).filter(s => !rowKey(s).f); }",
    "function lkRows() { return rowsOfSample(lkSample()); }",
)
# The one line P3b adds to the lookup's form, which says so.
LOOKUP_HTML_P3B = ('                <p class="ws-hint">Runs acquired with FAIMS are left out of these cohorts: there is no FAIMS '
                   'field here, so your run is compared with runs acquired without it.</p>\n')


def test_lookup_and_peg_server_are_byte_identical_to_ede086b(client):
    html, src = _page(client), APP_PATH.read_text()

    def between(h: str, a: str, b: str, incl: bool = False) -> str:
        i = h.index(a)
        j = h.index(b, i + len(a))
        return h[i:j + (len(b) if incl else 0)]
    got = {
        "lookup_js": between(html, "// ── Where does my run sit? (B1", "// ── Charts ──"),
        "lookup_html": between(html, '<div class="section" id="where">', '<div class="section" id="ranges">'),
        "lookup_css": between(html, "/* ── Community redesign P2c (relay 1.5.0) ──", "        .ws-hidden { display: none !important; }\n", True),
        "peg_server": between(src, "# ── PEG Watch: community PEG share channel (v1.2.0)", 'INDEX_HTML = r"""'),
    }
    assert got["lookup_js"].count(LOOKUP_P3B[0]) == 1
    got["lookup_js"] = got["lookup_js"].replace(*LOOKUP_P3B)
    assert got["lookup_html"].count(LOOKUP_HTML_P3B) == 1
    got["lookup_html"] = got["lookup_html"].replace(LOOKUP_HTML_P3B, "")
    assert {k: hashlib.sha256(v.encode()).hexdigest() for k, v in got.items()} == UNCHANGED_SINCE_EDE086B


# ── the Space image has no numpy ─────────────────────────────────────

_NO_NUMPY = r"""
import sys, io, json
sys.modules["numpy"] = None
import importlib.util, httpx, huggingface_hub, polars as pl
rows = json.loads(sys.argv[2])
buf = io.BytesIO(); pl.from_dicts(rows).write_parquet(buf)
def download(repo_id, filename, *a, **k):
    import tempfile, pathlib
    if filename != "benchmark_latest.parquet":
        from huggingface_hub.errors import RemoteEntryNotFoundError
        raise RemoteEntryNotFoundError("missing", response=httpx.Response(404, request=httpx.Request("GET", "https://x")))
    p = pathlib.Path(tempfile.mkdtemp()) / filename; p.write_bytes(buf.getvalue()); return str(p)
huggingface_hub.hf_hub_download = download
spec = importlib.util.spec_from_file_location("relay_tic_no_numpy", sys.argv[1])
mod = importlib.util.module_from_spec(spec); sys.modules["relay_tic_no_numpy"] = mod; spec.loader.exec_module(mod)
mod._ensure_flush_worker_started = lambda: None
from fastapi.testclient import TestClient
c = TestClient(mod.app)
s = c.get("/api/tic-summary"); assert s.status_code == 200, s.text
assert s.json()["cohorts"][0]["n"] == 6, s.text
assert c.get("/api/tic-traces", params=dict(sample="hela", mode="DIA", spd=100, lc="evosep")).json()["n"] == 6
print("TIC-NO-NUMPY-OK", flush=True)
import os; os._exit(0)
"""


def test_tic_summaries_need_no_numpy():
    rows = [_tic_row(i) for i in range(6)]
    for r in rows:
        r.pop("run_name")
    proc = subprocess.run([sys.executable, "-c", _NO_NUMPY, str(APP_PATH), json.dumps(rows)],
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "TIC-NO-NUMPY-OK" in proc.stdout
