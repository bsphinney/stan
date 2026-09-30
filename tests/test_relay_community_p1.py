"""Community site redesign, phase P1 ("correctness"), in the vendored relay.

Spec: docs/superpowers/specs/2026-09-29-community-redesign-and-precursor-lookup-design.md
(§A.3 D1-D4, §A.4 item 1, §A.6 P1); evidence: docs/community-redesign/REVIEW.md.

Server side: file names (run_name) and their hash (fingerprint) stay in the
stored rows but leave no public response (D4), run_name is optional at submit,
and SPACE_VERSION is 1.2.2. Page side, run in node against the page's own
script: DIA and DDA never share a cohort or a ranking (D1), counts say how
many runs and labs they hold (D2), no IPS badge or column (D3), no file name
in any hover (D4), and the fixes for bugs 6, 10, 11, 12 and 19.

The fakes and fixtures come from tests/test_relay_peg.py: every Hugging Face
call is replaced in memory and nothing here reaches the network.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.test_relay_peg import (  # noqa: F401  (hub, relay and client are fixtures)
    NODE,
    _page,
    client,
    hub,
    item,
    needs_node,
    relay,
    rows_of,
)

SECRET_NAME = "FL290926_HeLa50ng_stilcrap_KerryCol_MK.raw"
SECRET_PRINT = "feedfacecafebeef"


# ── server: SPACE_VERSION ────────────────────────────────────────────

def test_space_version_is_1_2_2(client):
    assert client.get("/api/version").json()["version"] == "1.2.3"
    assert "community site v1.2.3" in _page(client)


# ── server: D4, no file names in public responses ────────────────────

def _benchmark_parquet(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buf)
    return buf.getvalue()


def _stored_row(i: int, **over) -> dict:
    row = {
        "submission_id": f"sub-{i}",
        "display_name": "Clogged PeakTail",
        "instrument_family": "timsTOF",
        "instrument_model": "timsTOF HT",
        "acquisition_mode": "diapasef",
        "spd": 100,
        "amount_ng": 50.0,
        "cohort_id": "timsTOF_100spd_low",
        "n_precursors": 40000 + i,
        "n_peptides": 35000,
        "n_proteins": 5000,
        "n_psms": 0,
        "ips_score": 60,
        "run_name": f"{i}_{SECRET_NAME}",
        "fingerprint": f"{SECRET_PRINT}{i:02d}",
        "run_date": "2026-09-20T10:00:00+00:00",
        "sample_type": "hela",
        "is_flagged": False,
        "tic_rt_bins": json.dumps([0.05 + 0.1 * j for j in range(20)]),
        "tic_intensity": json.dumps([float(j % 7) for j in range(20)]),
    }
    row.update(over)
    return row


def _no_private(text: str) -> None:
    for needle in (SECRET_NAME, SECRET_PRINT, '"run_name"', '"fingerprint"'):
        assert needle not in text, needle


def test_leaderboard_keeps_file_names_and_fingerprints_server_side(client, hub):
    hub.files["benchmark_latest.parquet"] = _benchmark_parquet([_stored_row(i) for i in range(3)])
    r = client.get("/api/leaderboard")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    for row in body["submissions"]:
        assert "run_name" not in row and "fingerprint" not in row
        assert "tic_rt_bins" not in row and "tic_intensity" not in row
        # what the page shows instead of the file name is still there
        assert row["instrument_model"] == "timsTOF HT" and row["spd"] == 100
        assert row["run_date"].startswith("2026-09-20")
    _no_private(r.text)


def test_cohorts_strips_private_fields_at_any_depth(client, hub):
    hub.files["cohort_stats/cohort_percentiles_latest.json"] = json.dumps({
        "timsTOF_100spd_low": {
            "n_submissions": 2, "n_precursors": [40000, 41000],
            "run_name": [SECRET_NAME, "x.raw"], "fingerprint": [SECRET_PRINT],
            "rows": [{"n": 1, "run_name": SECRET_NAME, "fingerprint": SECRET_PRINT}],
        },
    }).encode()
    r = client.get("/api/cohorts")
    assert r.status_code == 200
    _no_private(r.text)
    c = r.json()["timsTOF_100spd_low"]
    assert c["n_submissions"] == 2 and c["n_precursors"] == [40000, 41000]
    assert c["rows"] == [{"n": 1}]


def test_no_public_get_carries_a_file_name(client, hub):
    """Spec §A.7: every read endpoint over the benchmark rows, in one sweep."""
    hub.files["benchmark_latest.parquet"] = _benchmark_parquet([_stored_row(i) for i in range(12)])
    hub.files["cohort_stats/cohort_percentiles_latest.json"] = json.dumps(
        {"timsTOF_100spd_low": {"run_name": [SECRET_NAME]}}).encode()
    for path in ("/api/leaderboard", "/api/cohorts", "/api/tic-overlay",
                 "/api/cohorts/timsTOF_100spd_low/tic", "/"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert SECRET_NAME not in r.text and SECRET_PRINT not in r.text, path


# ── server: run_name optional at submit ──────────────────────────────

V1_DIA = {
    "stan_version": "1.2.6",
    "schema_version": "v1.0.0",
    "fasta_md5": "8de1d9bd0a052b175f88f66f82500d92",
    "speclib_md5": "ad72bfb2730644c69147ba8f34bfe982",
    "library_coverage_pct": 70.1,
    "display_name": "Strip Test Lab",
    "instrument_family": "timsTOF",
    "instrument_model": "timsTOF HT",
    "acquisition_mode": "diapasef",
    "spd": 100,
    "gradient_length_min": 11,
    "cohort_id": "timsTOF_100spd_low",
    "n_precursors": 40000,
    "n_peptides": 35000,
    "n_proteins": 5000,
    "ips_score": 61,
    "median_mass_acc_ms1_ppm": 1.2,
    "median_mass_acc_ms2_ppm": 2.1,
    "ms1_signal": 1.5e10,
    "ms2_signal": 2.5e10,
    "median_peak_width_sec": 3.1,
    "median_points_across_peak": 9.0,
    "dynamic_range_log10": 3.6,
    "fwhm_rt_min": 0.05,
    "peak_capacity": 150.0,
    "column_vendor": "Evosep",
    "column_model": "EV1109",
    "lc_system": "evosep",
    "diann_version": "2.3.0",
    "fingerprint": "0011223344556677",
    "run_name": "",
    "run_date": "2026-09-28T10:00:00+00:00",
    "tic_rt_bins": [0.05, 0.15, 0.25],
    "tic_intensity": [1.0, 3.0, 2.0],
}
V1_DDA = {
    "stan_version": "1.2.6",
    "schema_version": "v1.0.0",
    "fasta_md5": "8de1d9bd0a052b175f88f66f82500d92",
    "display_name": "Strip Test Lab",
    "instrument_family": "Exploris",
    "instrument_model": "Orbitrap Exploris 480",
    "acquisition_mode": "dda",
    "spd": 38,
    "cohort_id": "Exploris_30spd_low",
    "n_psms": 21000,
    "n_peptides": 16000,
    "n_proteins": 3400,
    "ips_score": 40,
    "column_vendor": "Unknown",
    "column_model": "Unknown",
    "fingerprint": "8899aabbccddeeff",
    "run_name": "",
    "run_date": "2026-09-28T10:00:00+00:00",
}


@pytest.mark.parametrize("payload", [V1_DIA, V1_DDA], ids=["dia", "dda"])
@pytest.mark.parametrize("how", ["empty", "absent"])
def test_submit_is_accepted_without_a_run_name(client, relay, payload, how):
    """STAN_STRIP_RUN_NAME sends it empty; relay 1.2.1 refused that with 422."""
    body = dict(payload)
    if how == "absent":
        del body["run_name"]
    r = client.post("/api/submit", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    [stored] = rows_of(item(relay, "submissions/").data)
    assert stored["run_name"] == ""
    assert stored["fingerprint"] == payload["fingerprint"]   # dedupe key still stored


def test_submit_with_a_run_name_still_stores_it(client, relay):
    """Kept server-side for dedupe and /api/update (D4); only responses drop it."""
    r = client.post("/api/submit", json=dict(V1_DIA, run_name=SECRET_NAME))
    assert r.status_code == 200, r.text
    _no_private(r.text)
    [stored] = rows_of(item(relay, "submissions/").data)
    assert stored["run_name"] == SECRET_NAME


def test_the_v1_completeness_gate_still_refuses_other_missing_fields(client, relay):
    r = client.post("/api/submit", json=dict(V1_DIA, fasta_md5="", run_name=""))
    assert r.status_code == 422
    assert "fasta_md5" in r.json()["detail"] and "run_name" not in r.json()["detail"]
    assert "run_name" not in relay.V1_REQUIRED_DIA_STR | relay.V1_REQUIRED_DDA_STR


# ── page text ────────────────────────────────────────────────────────

def test_page_text_p1(client):
    html = _page(client)
    # D2: disclosure line, computed banner count, no community-consensus claims
    assert ("Today essentially every run here comes from one facility, the UC Davis Proteomics Core "
            "(timsTOF HT, Exploris 480, Fusion Lumos). The ranges below are that facility's "
            "longitudinal ranges until more labs join.") in html
    assert "3,800+" not in html and 'id="banner-runs"' in html
    assert "established by the community" not in html
    assert "First-of-its-kind" not in html and "cross-lab TIC" not in html
    # D3: IPS card is v2, badges and the two dropped charts are gone
    assert "30% precursor depth" not in html and "hyperscore" not in html
    assert "50%</strong> precursors (PSMs for DDA)" in html and "359 UC Davis HeLa QC runs" in html
    assert "IPS is not shown on this page yet" in html
    assert "context only; not used for leaderboards; 20% of IPS" in html
    assert "Health:</strong> IPS" not in html
    for gone in ("Identification Depth vs. IPS", "chart-ips", "renderGrs",
                 "Instrument Health Fingerprint", "chart-radar", "renderRadar",
                 "ipsBadge", 'value="ips_score"', "&lt;60 Investigate"):
        assert gone not in html, gone
    # D4: the page never reads a file name
    main = _main_script(html)
    assert "run_name" not in main and "fingerprint" not in main
    # bug 6: both Matthews & Hayes links, thresholds as STAN guidelines
    assert html.count("https://doi.org/10.1021/ac50003a028") >= 3
    assert "ac50005a009" not in html and "ac50012a005" not in html
    assert "Min for <1% error" not in html and "quantitation error exceeds 1%" not in html
    assert "STAN's guideline" in html or "STAN\\'s guideline" in html
    # bug 19, TIC wording, D7 library wording, decision 7 footer
    assert "Populated going forward" not in html
    assert "Identified (DIA) or raw (DDA)" not in html
    assert "MS1 total-ion chromatograms read from the raw file" in html
    assert "predicted spectral library" not in html
    assert "empirical HeLa libraries, one per vendor (timsTOF ~54k, Orbitrap ~170k precursors)" in html
    assert "opensource.org/licenses/MIT" not in html and "Code: <a" in html
    assert ('Code: <a href="https://github.com/bsphinney/stan/blob/main/LICENSE">STAN Academic License</a> '
            "(free for academic and non-profit use; commercial use by written permission)") in html


# ── page behaviour, in node ──────────────────────────────────────────

_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = fs.readFileSync(process.argv[3], 'utf8');
// Just enough DOM: one persistent element per id; a <select> keeps its
// options and falls back to the first one, as a browser does.
class El {
    constructor(id) {
        this.id = id; this.style = {}; this.textContent = ''; this.value = ''; this.dataset = {};
        this.disabled = false; this.checked = false; this.options = []; this._html = '';
        this.classList = { add() {}, remove() {}, contains: () => false };
    }
    get innerHTML() { return this._html; }
    set innerHTML(h) {
        this._html = String(h);
        const vals = [...this._html.matchAll(/<option(?: value="([^"]*)")?[^>]*>([^<]*)<\/option>/g)]
            .map(m => (m[1] !== undefined ? m[1] : m[2]));
        this.options = vals.map(v => ({ value: v }));
        if (vals.length && !vals.includes(this.value)) this.value = vals[0];
    }
    addEventListener() {} appendChild() {} setAttribute() {}
    querySelector() { return null; } querySelectorAll() { return []; }
}
const els = {};
const byId = (id) => (els[id] = els[id] || new El(id));
const plots = [];
// Every renderer runs inside try/catch and reports through console.error,
// so an exception would otherwise pass silently: collect them.
const errors = [];
const con = { log() {}, info() {}, warn() {}, error: (...a) => errors.push(a.map(x => (x && x.stack) || String(x)).join(' ')) };
const ctx = vm.createContext({
    console: con, els, plots,
    document: { getElementById: byId, querySelector: () => null, querySelectorAll: () => [],
                addEventListener() {}, createElement: () => new El(''), body: new El('body') },
    window: { addEventListener() {}, innerWidth: 1280 },
    fetch: () => new Promise(() => {}),       // the page's own load never settles here
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0,
    Plotly: { newPlot: (id, traces, layout) => plots.push({ id: typeof id === 'string' ? id : id.id, traces, layout }),
              Plots: { resize() {} }, purge() {} },
});
vm.runInContext(src, ctx);
const out = vm.runInContext(scenario, ctx);
process.stdout.write(JSON.stringify({ out, errors }));
process.exit(0);
"""


def _main_script(html: str) -> str:
    return next(b for b in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "function renderConfigLeaderboard" in b)


def _run(client, tmp_path: Path, scenario: str):
    html = _page(client)
    esc_block = re.search(r'<script id="stan-esc">(.*?)</script>', html, re.S).group(1)
    (tmp_path / "main.js").write_text(esc_block + "\n" + _main_script(html))
    (tmp_path / "scenario.js").write_text(scenario)
    (tmp_path / "harness.js").write_text(_HARNESS)
    proc = subprocess.run([NODE, str(tmp_path / "harness.js"), str(tmp_path / "main.js"),
                           str(tmp_path / "scenario.js")], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-3000:]
    got = json.loads(proc.stdout)
    assert got["errors"] == [], got["errors"]
    return got["out"]


def _row(i: int, **over) -> dict:
    """A page row as /api/leaderboard serves it, plus a file name the page must never show."""
    row = {
        "submission_id": f"s{i}", "display_name": "Clogged PeakTail",
        "instrument_family": "timsTOF", "instrument_model": "timsTOF HT",
        "acquisition_mode": "diapasef", "spd": 100, "amount_ng": 50, "cohort_id": "timsTOF_100spd_low",
        "n_precursors": 40000 + 100 * i, "n_peptides": 35000 + 50 * i, "n_proteins": 5000, "n_psms": 0,
        "ips_score": 55, "median_points_across_peak": 9.0, "median_mass_acc_ms1_ppm": 1.1,
        "ms1_signal": 1e12, "dynamic_range_log10": 3.5, "column_vendor": "Unknown", "column_model": "Unknown",
        "sample_type": "hela", "is_flagged": False, "run_date": f"2026-09-{1 + i % 28:02d}T10:00:00Z",
        "run_name": f"{SECRET_NAME}_{i}",
    }
    row.update(over)
    return row


def _dda(i: int, **over) -> dict:
    return _row(i, **{"acquisition_mode": "dda", "n_precursors": 0, "n_psms": 30000 + 100 * i, **over})


@needs_node
def test_ordinals(client, tmp_path):
    got = _run(client, tmp_path, "[1,2,3,4,11,12,13,21,22,23,93,100,101,111,112,113].map(ordinal)")
    assert got == ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd",
                   "93rd", "100th", "101st", "111th", "112th", "113th"]


@needs_node
def test_reference_cards_keep_dda_out_of_dia_cohorts(client, tmp_path):
    """REVIEW D1 / bug 1: 11 DDA rows in a DIA cohort made its card read "0 - 37,360"."""
    rows = [_row(i) for i in range(12)] + [_dda(100 + i) for i in range(5)]
    html = _run(client, tmp_path, f"allData = {json.dumps(rows)}; renderRefRanges(); els['ref-ranges-container'].innerHTML")
    cards = html.split('<div class="ref-card"')[1:]
    assert len(cards) == 2
    dia, dda = cards
    assert "timsTOF HT · 100 SPD · 26-75 ng · DIA" in dia and "12 runs · 1 lab" in dia
    assert "Precursors (IQR)" in dia and "PSMs" not in dia
    m = re.search(r'Precursors \(IQR\)</span><span class="ref-range">([^<]+)<', dia)
    assert m and not m.group(1).startswith("0"), m and m.group(1)
    # below 10 runs the values are listed, not a range; one lab gets the tag
    assert "· DDA" in dda and "5 runs · 1 lab" in dda and "PSMs (values)" in dda
    assert "40,000 · 40,100 · 40,200 · 40,300 · 40,400" in dda
    assert dda.count("single-lab reference") == 1 and dia.count("single-lab reference") == 1
    # D3: no IPS on a card
    assert "IPS" not in html


@needs_node
def test_two_labs_lose_the_single_lab_tag(client, tmp_path):
    rows = [_row(i, display_name="Lab A" if i % 2 else "Lab B") for i in range(12)]
    html = _run(client, tmp_path, f"allData = {json.dumps(rows)}; renderRefRanges(); els['ref-ranges-container'].innerHTML")
    assert "12 runs · 2 labs" in html and "single-lab reference" not in html


def _config_rows() -> list[dict]:
    rows = []
    # DDA: Lumos 15 SPD, two labs, lowest PSMs; timsTOF HT 30 SPD, two labs, most PSMs;
    # Exploris 60 SPD, one lab, middle.
    for i, (psms, lab) in enumerate([(20000, "A"), (21000, "B"), (22000, "A")]):
        rows.append(_dda(i, instrument_model="Orbitrap Fusion Lumos", instrument_family="Lumos", spd=15,
                         cohort_id="Lumos_15spd_low", n_psms=psms, n_peptides=30000, display_name=lab))
    for i, (psms, lab) in enumerate([(50000, "A"), (52000, "B"), (51000, "A")]):
        rows.append(_dda(10 + i, spd=30, cohort_id="timsTOF_30spd_low", n_psms=psms, n_peptides=10000,
                         display_name=lab))
    for i, psms in enumerate([30000, 31000, 32000]):
        rows.append(_dda(20 + i, instrument_model="Orbitrap Exploris 480", instrument_family="Exploris",
                         spd=60, cohort_id="Exploris_60spd_low", n_psms=psms, n_peptides=50000))
    # DIA, one lab
    rows += [_row(30 + i) for i in range(4)]
    return rows


@needs_node
def test_best_configurations_under_dda_rank_by_psms(client, tmp_path):
    """REVIEW D1 / bug 3: DDA stayed "sorted by precursors" and badged 24,636 PSMs over 52,085."""
    scenario = f"""(() => {{
        allData = {json.dumps(_config_rows())};
        currentTab = 'dia';
        sortConfigLeaderboard('peptides', 'DIA');         // a reader re-sorts the DIA table ...
        sortConfigLeaderboard('peptides', 'DDA');         // ... and the DDA one
        event = {{ target: document.getElementById('tab-dda') }};  // showTab() reads window.event
        showTab('dda');                                   // the tab switch resets both
        const html = els['config-leaderboard'].innerHTML;
        return {{ html, badge: els['config-leaderboard-badge'].textContent,
                  sort: JSON.parse(JSON.stringify(configSort)) }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    html = got["html"]
    assert got["sort"] == {"DIA": {"col": "precursors", "asc": False}, "DDA": {"col": "psms", "asc": False}}
    assert got["badge"] == "HELA · DDA · sorted by psms"
    assert ">Precursors" not in html and ">PSMs ▼" in html and ">Labs" in html
    order = re.findall(r'font-weight:600">([^<]+)</span>', html)
    assert order == ["timsTOF HT", "Orbitrap Exploris 480", "Orbitrap Fusion Lumos"]
    # best depth is the maximum PSMs row (two labs), never a one-lab row
    assert html.count("best depth") == 1
    assert re.search(r'timsTOF HT</span> <span[^>]*>best depth', html)
    assert "single-lab reference" in html   # the Exploris row


@needs_node
def test_best_configurations_one_lab_row_gets_no_best_badge(client, tmp_path):
    rows = [r for r in _config_rows() if r["instrument_family"] == "Exploris"]
    html = _run(client, tmp_path, f"allData = {json.dumps(rows)}; currentTab = 'dda'; renderConfigLeaderboard(); els['config-leaderboard'].innerHTML")
    assert "best depth" not in html and "best accuracy" not in html


@needs_node
def test_best_configurations_under_all_shows_two_tables(client, tmp_path):
    html = _run(client, tmp_path, f"allData = {json.dumps(_config_rows())}; currentTab = 'all'; renderConfigLeaderboard(); els['config-leaderboard'].innerHTML")
    assert html.count("<table") == 2
    dia, dda = html.split("<table")[1:]
    assert "DIA · ranked by precursors" in html and "DDA · ranked by psms" in html
    assert ">Precursors ▼" in dia and ">PSMs" not in dia
    assert ">PSMs ▼" in dda and ">Precursors" not in dda


@needs_node
def test_submissions_table_has_no_ips_and_ranks_each_track_on_its_own(client, tmp_path):
    rows = [_row(i) for i in range(4)] + [_dda(10 + i, cohort_id="timsTOF_100spd_low") for i in range(4)]
    html = _run(client, tmp_path, f"allData = {json.dumps(rows)}; currentTab = 'all'; renderTable(); els['table-container'].innerHTML")
    assert ">IPS<" not in html and "IPS " not in html
    assert "Precursors / PSMs" in html
    # a DDA row shows its PSMs, not 0 precursors, and ranks among DDA rows only
    assert "<strong>31,300</strong>" in html and "<strong>0</strong>" not in html
    assert "100th" not in html and "75th" in html


@needs_node
def test_no_file_name_in_any_chart_hover(client, tmp_path):
    rows = [_row(i) for i in range(12)] + [_dda(100 + i) for i in range(4)]
    scenario = f"""(() => {{
        allDataRaw = {json.dumps(rows)}; applyFilters();
        renderCharts();
        document.getElementById('lab-select').value = 'Clogged PeakTail';
        renderLabVsCommunity();
        renderLeveyJennings(allData, 'n_precursors', 'timsTOF HT');   // not on the page, but fixed too
        return plots;
    }})()"""
    plots = _run(client, tmp_path, scenario)
    ids = {p["id"] for p in plots}
    assert {"chart-mass-acc", "chart-lab-trend", "chart-lj"} <= ids
    assert SECRET_NAME not in json.dumps(plots)
    [scatter] = [t for p in plots if p["id"] == "chart-mass-acc" for t in p["traces"]]
    assert scatter["text"][0] == "timsTOF HT<br>2026-09-01<br>100 SPD"
    assert not ids & {"chart-ips", "chart-radar"}


@needs_node
def test_lab_trend_clears_a_stale_empty_state(client, tmp_path):
    """Bug 10: E. coli -> HeLa left "Not enough community data" under the drawn plot."""
    rows = [_row(i) for i in range(12)]
    scenario = f"""(() => {{
        const sel = document.getElementById('lab-select');
        allData = []; renderLabVsCommunity();
        allData = {json.dumps([r for r in rows[:1]])}; sel.value = 'Clogged PeakTail';
        renderLabVsCommunity();
        const before = els['chart-lab-trend'].innerHTML;
        allData = {json.dumps(rows)}; sel.value = 'Clogged PeakTail';
        renderLabVsCommunity();
        return {{ before, after: els['chart-lab-trend'].innerHTML,
                  plotted: plots.filter(p => p.id === 'chart-lab-trend').length,
                  note: plots.length ? plots[plots.length - 1].layout.annotations[0].text : '' }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert "Not enough community data" in got["before"]
    assert got["after"] == "" and got["plotted"] == 1
    assert "12 runs · 1 lab" in got["note"]


def _tic(i: int, spd: int, idion: bool = False, **over) -> dict:
    start = 2.0 if idion else 0.05          # an identified-ion trace starts at the first ID
    rt = [round(start + 0.1 * j, 3) for j in range(30)]
    y = [float(1 + (j % 10)) for j in range(30)]
    return _row(i, spd=spd, lc_system="evosep", tic_rt_bins=json.dumps(rt), tic_intensity=json.dumps(y), **over)


def _tic_rows() -> list[dict]:
    rows = [_tic(i, 7) for i in range(2)]                       # the lowest SPD: 2 runs
    rows += [_tic(10 + i, 100) for i in range(6)]               # the largest cohort
    rows += [_tic(20 + i, 60) for i in range(3)] + [_tic(30, 60, idion=True)]
    return rows


@needs_node
def test_tic_opens_on_the_largest_cohort_with_bands(client, tmp_path):
    scenario = f"""(() => {{
        ticLoaded = true; allData = {json.dumps(_tic_rows())};
        renderCommunityTIC();
        const p = plots[plots.length - 1];
        return {{ sel: els['tic-spd-select'].value, opts: els['tic-spd-select'].innerHTML,
                  count: els['tic-count'].innerHTML, names: p.traces.map(t => t.name) }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert got["sel"] == "100"
    assert "7 SPD (2 runs, no bands)" in got["opts"] and "100 SPD (6 runs)" in got["opts"]
    assert "60 SPD (3 runs + 1 identified-ion, no bands)" in got["opts"]
    assert "6 runs · 1 lab" in got["count"] and "single-lab reference" in got["count"]
    assert "Median (6 runs · 1 lab)" in got["names"] and "25–75th pct (IQR)" in got["names"]


@needs_node
def test_tic_below_five_runs_draws_each_run_and_keeps_identified_ion_out(client, tmp_path):
    scenario = f"""(() => {{
        ticLoaded = true; allData = {json.dumps(_tic_rows())};
        renderCommunityTIC();
        document.getElementById('tic-spd-select').value = '60';
        renderCommunityTIC();
        const p = plots[plots.length - 1];
        return {{ count: els['tic-count'].innerHTML,
                  traces: p.traces.map(t => ({{ name: t.name, visible: t.visible === undefined ? true : t.visible,
                                                 dash: (t.line || {{}}).dash || '' }})) }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert "each of 3 runs (too few for a median)" in got["count"]
    assert "1 identified-ion trace kept out of the median" in got["count"]
    names = [t["name"] for t in got["traces"]]
    assert not any(n.startswith("Median") or "pct" in n for n in names)
    assert names.count("timsTOF HT") == 3
    [idion] = [t for t in got["traces"] if t["dash"] == "dot"]
    assert idion["name"] == "Identified-ion traces (1)" and idion["visible"] == "legendonly"


@needs_node
def test_tic_dda_says_none_submitted_and_turns_its_controls_off(client, tmp_path):
    scenario = f"""(() => {{
        ticLoaded = true; allDataRaw = allData = {json.dumps(_tic_rows())};
        renderCommunityTIC();
        document.getElementById('tic-mode-select').value = 'dda';
        renderCommunityTIC();
        return {{ msg: els['chart-community-tic'].innerHTML,
                  off: ['tic-spd-select', 'tic-lc-select', 'tic-show-all'].map(i => els[i].disabled),
                  mode: els['tic-mode-select'].disabled }};
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert "No DDA TIC traces have been submitted yet." in got["msg"]
    assert got["off"] == [True, True, True] and got["mode"] is False


@needs_node
def test_banner_counts_the_whole_seed_and_the_tile_follows_the_filter(client, tmp_path):
    """No hard-coded "3,800+" (D2), and no "Seeded with 0" under a QC standard
    with no runs: the banner counts every standard, the tile the filtered set."""
    rows = [_row(i) for i in range(3)] + [_row(10 + i, sample_type="yeast") for i in range(2)]
    scenario = f"""(() => {{
        allDataRaw = {json.dumps(rows)}; applyFilters(); updateStats();
        return [els['banner-runs'].textContent, els['stat-submissions'].textContent];
    }})()"""
    assert _run(client, tmp_path, scenario) == ["5", "3"]


@needs_node
def test_default_lab_name_is_not_a_second_lab(client, tmp_path):
    """'Anonymous Lab' is the default name for any unnamed submitter: it must
    never turn a one-lab cohort into "2 labs" (which unlocks "best" badges)."""
    scenario = """[
      labCount([{display_name:'Clogged PeakTail'},{display_name:'Anonymous Lab'}]),
      labCount([{display_name:'Anonymous Lab'},{display_name:'Anonymous Lab'}]),
      labCount([{display_name:'Clogged PeakTail'},{display_name:'Nimble Edman'},{display_name:'Anonymous Lab'}]),
      labCount([])
    ]"""
    assert _run(client, tmp_path, scenario) == [1, 1, 2, 0]


@needs_node
def test_best_configurations_default_name_is_not_a_second_lab(client, tmp_path):
    """Review finding 1: 'Clogged PeakTail' + 'Anonymous Lab' (both UC Davis) read
    "2 labs" and unlocked a "best accuracy" badge under the one-facility banner."""
    rows = []
    for i in range(6):
        for name in ("Clogged PeakTail", "Anonymous Lab"):
            rows.append(_row(len(rows), display_name=name, instrument_family="Exploris",
                             instrument_model="Orbitrap Exploris 480", spd=38,
                             median_mass_acc_ms1_ppm=0.4 + 0.01 * i))
    html = _run(client, tmp_path, f"allData = {json.dumps(rows)}; currentTab = 'dia'; renderConfigLeaderboard(); els['config-leaderboard'].innerHTML")
    assert "single-lab reference" in html
    assert "best depth" not in html and "best accuracy" not in html


# ── Review fixes before the P1 deploy (2026-09-29) ────────────────────


def test_error_reports_are_admin_only(client, monkeypatch):
    """Error reports carry raw file names and full command lines (D4)."""
    monkeypatch.setenv("ADMIN_SECRET", "s3cret")
    assert client.get("/api/error-reports").status_code == 403
    assert client.get("/api/error-reports", headers={"X-STAN-Admin": "nope"}).status_code == 403
    r = client.get("/api/error-reports", headers={"X-STAN-Admin": "s3cret"}, params={"limit": 10_000})
    assert r.status_code == 200


def test_error_reports_closed_without_an_admin_secret(client, monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    assert client.get("/api/error-reports").status_code == 403


def test_update_owner_check(relay, monkeypatch):
    """Before this, any non-empty X-STAN-Auth could rewrite any row."""
    HTTPException = relay.HTTPException
    owners = {("Clogged PeakTail", "good-token"), ("Nimble Edman", "good-token")}

    def fake_identity(name, token):
        if (name, token) in owners:
            return True
        if name in {"Clogged PeakTail", "Nimble Edman"}:
            raise HTTPException(status_code=403, detail="claimed")
        return False  # unclaimed: accepted-but-unverified for PEG, not ownership

    monkeypatch.setattr(relay, "_peg_identity", fake_identity)
    check = relay._update_owner_check
    check(True, "", "Anyone", {"spd": 60})                      # admin
    check(False, "good-token", "Clogged PeakTail", {"spd": 60})  # owner
    check(False, "good-token", "Clogged PeakTail", {"display_name": "Nimble Edman"})
    for args in [
        (False, "", "Clogged PeakTail", {"spd": 60}),             # no token
        (False, "bad-token", "Clogged PeakTail", {"spd": 60}),    # wrong token
        (False, "good-token", "Anonymous Lab", {"spd": 60}),      # unclaimed row
        (False, "good-token", "Clogged PeakTail", {"display_name": "Someone Else"}),
    ]:
        with pytest.raises(HTTPException) as e:
            check(*args)
        assert e.value.status_code == 403

    def outage(name, token):
        raise HTTPException(status_code=503, detail="registry down")

    monkeypatch.setattr(relay, "_peg_identity", outage)
    with pytest.raises(HTTPException) as e:
        check(False, "good-token", "Clogged PeakTail", {"spd": 60})
    assert e.value.status_code == 503


@needs_node
def test_submissions_table_escapes_submitter_strings(client, tmp_path):
    rows = [_row(1, instrument_model='<img src=x onerror=alert(1)>',
                 column_vendor='<b>v</b>', column_model='<script>x</script>', spd='<i>60</i>')]
    html = _run(client, tmp_path, f"allData = {json.dumps(rows)}; currentTab = 'all'; renderTable(); els['table-container'].innerHTML")
    assert "<img" not in html and "<script>" not in html and "<i>60" not in html
    assert "&lt;img" in html


@needs_node
def test_all_tab_never_ranks_psms_against_precursors(client, tmp_path):
    rows = [_row(i) for i in range(3)] + [_dda(10 + i, n_psms=90000 + i) for i in range(3)]
    html = _run(client, tmp_path, f"allData = {json.dumps(rows)}; currentTab = 'all'; tableSortCol = null; renderTable(); els['table-container'].innerHTML")
    body = html.split("<tbody>")[1]
    first_dda = body.find("badge-dda")
    last_dia = body.rfind("badge-dia")
    assert 0 <= last_dia < first_dda, "DIA rows must all come before DDA rows under All"


def test_duplicate_reply_names_the_existing_submission(client, relay, monkeypatch):
    """Space 1.2.3: the 409 names the row the relay already has, so the client
    records the run as submitted with that id instead of re-sending it."""
    import polars as pl

    sid = "0f3c2a4e-1111-2222-3333-444455556666"
    existing = pl.DataFrame({"fingerprint": [V1_DIA["fingerprint"]], "submission_id": [sid]})
    monkeypatch.setattr(relay, "_load_all_submissions", lambda *a, **k: existing)
    again = client.post("/api/submit", json=V1_DIA)
    assert again.status_code == 409
    assert f"Existing submission_id: {sid}." in again.json()["detail"]
