"""Community site redesign, phase P2a (layout and cards), in the vendored relay.

Spec: docs/superpowers/specs/2026-09-29-community-redesign-and-precursor-lookup-design.md
(§A.1 page order, §A.2 chart inventory, §A.3 D5-D8 and B6, §A.6 P2);
mockup: docs/community-redesign/mockup/ (v3.1, approved 2026-09-29).

Server side: SPACE_VERSION 1.3.0 and a /favicon.ico route. Page side, as
text: the page order, the nav anchors, the stats row with its Join tile and
the D8 note, the glossary, the Join and Methods cards (checked against the
code they describe). Page side, run in node against the page's own script
(the harness in tests/test_relay_community_p1.py): the reference cards
grouped by model with sparse cohorts folded, colKey() and "Unknown", the
read-time dedupe and held-back amounts, the Best Configurations amount
select, the violins per SPD cohort, Depth by Throughput per model, the
column comparison's empty state, and the ID-free charts per model.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

from tests.test_relay_community_p1 import _dda, _row, _run
from tests.test_relay_peg import _page, client, hub, needs_node, relay  # noqa: F401  (fixtures)

REPO = Path(__file__).resolve().parents[1]
EVIL = '<img src=x onerror="alert(1)">'


def _section(html: str, sid: str) -> str:
    """The HTML of <div class="section" id=sid> up to the next top-level section."""
    start = html.index(f'<div class="section" id="{sid}">')
    nxt = html.find('\n<div class="section"', start + 10)
    return html[start:nxt if nxt > 0 else len(html)]


# ── server ───────────────────────────────────────────────────────────

def test_space_version_is_1_3_0_or_later(client, relay):
    # P2a shipped as 1.3.0; P2b (tests/test_relay_community_p2b.py) is 1.4.0.
    assert relay.SPACE_VERSION == "1.4.0"
    assert client.get("/api/version").json()["version"] == "1.4.0"
    assert "community site v1.4.0" in _page(client)


def test_favicon_is_served_and_inline(client, relay):
    """Bug 21: /favicon.ico was a 404 on every page; Chrome warned about the
    Apple-only web-app meta."""
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.text == relay.FAVICON_SVG
    assert "/favicon.ico" not in client.get("/openapi.json").json()["paths"]
    html = _page(client)
    head = html[:html.index("</head>")]
    m = re.search(r'<link rel="icon" type="image/svg\+xml" href="data:image/svg\+xml,([^"]+)">', head)
    assert m, "inline SVG icon in <head>"
    assert urllib.parse.unquote(m.group(1)) == relay.FAVICON_SVG
    assert '<meta name="mobile-web-app-capable" content="yes">' in head


# ── page text: order, nav, stats, glossary ───────────────────────────

NAV = [("Join", "#join"), ("Where do I stand", "#where"), ("Instrument Health Explorer", "#explore"),
       ("Methods", "#methods"), ("PEG Watch", "#peg"),
       ("Dataset", "https://huggingface.co/datasets/brettsp/stan-benchmark"), ("API", "/docs"),
       ("GitHub", "https://github.com/bsphinney/stan"), ("Museum", "/museum"), ("Arcade", "/arcade")]


def test_nav_order_and_every_anchor_has_a_target(client):
    html = _page(client)
    nav = html[html.index('<nav class="nav"'):html.index("</nav>")]
    links = [(re.sub(r"&#\d+;|\s+", " ", text).strip(), href)
             for href, text in re.findall(r'<a (?:class="join" )?href="([^"]+)">(.*?)</a>', nav)]
    assert links == NAV
    assert '<a class="join" href="#join">Join</a>' in nav
    for _, href in NAV:
        if href.startswith("#"):
            assert html.count(f'id="{href[1:]}"') == 1, href


def test_page_order_follows_spec_a1(client):
    """§A.1: header, stats, glossary, reference ranges, Join, Methods, the
    Explorer (every §A.2 chart), ID-free health, lab trend, PEG, submissions,
    footer."""
    html = _page(client)
    body = html[html.index("<body>"):]
    order = ['class="purpose"', 'id="stats"', 'id="gloss"', 'id="sample-type-select"', 'id="where"',
             'id="join"', 'id="methods"', 'id="explore"', 'id="best"', 'id="chart-amount-depth"',
             'id="chart-violin"', 'id="chart-spd-depth"', 'id="row-column-compare"', 'id="chart-points-peak"',
             'id="chart-community-tic"', 'id="health"', 'id="chart-mass-acc"', 'id="chart-ms1-signal"',
             'id="chart-dyn-range"', 'id="chart-pts-peak"', 'id="chart-lab-trend"', 'id="peg"',
             'id="submissions"', 'class="footer"']
    pos = [body.index(m) for m in order]
    assert pos == sorted(pos), [m for m, p in zip(order, pos)]
    # Every chart §A.2 keeps is on the page once; the two Brett dropped are not.
    for chart in ("config-leaderboard", "chart-amount-depth", "chart-violin", "chart-spd-depth",
                  "chart-column-compare", "chart-points-peak", "chart-community-tic", "chart-mass-acc",
                  "chart-ms1-signal", "chart-dyn-range", "chart-pts-peak", "chart-lab-trend", "chart-peg-trend",
                  "ref-ranges-container"):
        assert body.count(f'id="{chart}"') == 1, chart
    for gone in ("chart-ips", "chart-radar", "Understanding the Metrics"):
        assert gone not in body
    # Column Comparison is never hidden any more.
    assert "display = on ? '' : 'none'" not in html and "setVisible(false)" not in html
    # The section keeps its live name.
    assert "<h2>Instrument Health Explorer</h2>" in _section(html, "explore")


def test_header_stats_and_glossary(client):
    html = _page(client)
    assert ('<p class="purpose">Compare your QC HeLa against reference ranges from labs running '
            'the same frozen search.</p>') in html
    # The inert "Hide failed runs · 0 flagged" card is gone; a Join tile has its place (D6).
    shown = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    for gone in ("Hide failed runs", "hide-failed-cb", "toggleFailedFilter", "stat-failed", "Seeded with"):
        assert gone not in shown, gone
    stats = html[html.index('id="stats"'):html.index('id="gloss"')]
    assert '<a class="stat-card stat-join" href="#join">' in stats
    assert "Your runs appear after the nightly rebuild." in stats
    for tile in ("stat-submissions", "stat-labs", "stat-instruments", "stat-latest", "stats-note"):
        assert f'id="{tile}"' in stats, tile
    gloss = html[html.index('id="gloss"'):html.index("</div>\n</div>", html.index('id="gloss"'))]
    assert "<b>SPD</b>samples per day. For Evosep it names the method you ran; for nanoLC it is 1440" in gloss
    assert "<b>Middle half (IQR)</b>the range holding the middle 50% of runs" in gloss
    assert "<b>IPS</b>a 0&ndash;100 depth score" in gloss and "Not shown on this page until a scoring fix ships" in gloss


# ── Join (D6) and Methods (D7 + D3), checked against the code ─────────

def test_join_card(client):
    html = _page(client)
    join = _section(html, "join")
    steps = join[join.index('<ol class="steps">'):join.index("</ol>")]
    order = ["https://github.com/bsphinney/stan/blob/main/INSTALL_FOR_AGENTS.md", "stan community-claim",
             "community_submit: true", "stan submit-all"]
    assert [steps.index(x) for x in order] == sorted(steps.index(x) for x in order)
    assert "Pierce HeLa" in steps and "88328" in steps
    # A fresh `stan init` leaves display_name empty and community-claim then
    # refuses to run, so the step names the lab first, in the real files.
    assert steps.index("display_name") < steps.index("stan community-claim")
    assert 'display_name: ""' in (REPO / "stan/setup.py").read_text()
    assert "no community lab name to claim" in (REPO / "stan/cli.py").read_text()
    config = (REPO / "stan/config.py").read_text()
    assert 'Path.home() / "STAN"' in config and 'Path.home() / ".stan"' in config
    assert "~/.stan/community.yml" in steps and "%USERPROFILE%\\STAN\\community.yml" in steps
    assert "stan setup" in steps
    assert "nightly rebuild at 04:00 UTC" in join
    # Not a promise the data cannot keep until the decision-11 re-search.
    assert "compare directly" not in join and "close but not yet exact" in join
    # The rebuild the card promises is the consolidation cron.
    wf = (REPO / ".github/workflows/consolidate_benchmark.yml").read_text()
    assert "cron: '0 4 * * *'" in wf
    assert 'id="join-fields"' in join and '<details class="fieldlist">' in join and 'id="fl-list"' in join
    for kept in ("Raw data and spectra", "Instrument serial numbers", "Never leaves your lab"):
        assert kept in join
    # Honest about file names: never shown here, but still in the public dataset (decision 5b pending).
    assert "This page and its API never show it" in join and "public Hugging Face dataset" in join
    assert "STAN_STRIP_RUN_NAME=1" in join
    assert '<a href="#join-fields">Every published field</a>' in html[html.index('class="footer"'):]
    # The page itself never names the private fields.
    assert '"run_name"' not in html and '"fingerprint"' not in html


def test_methods_card_matches_the_code(client, relay):
    """D7: every version, parameter, checksum and count definition on the card
    is read off the code that produces the numbers."""
    from stan.community.validate import EXPECTED_ASSET_HASHES
    from stan.search.community_params import (
        COMMUNITY_DIANN_PARAMS_FROZEN, COMMUNITY_SAGE_PARAMS, PINNED_TOOL_VERSIONS, SEARCH_PARAMS_VERSION,
    )

    methods = _section(_page(client), "methods")
    text = re.sub(r"<[^>]+>", "", methods).replace("&ndash;", "–").replace("&le;", "≤")
    assert f"SEARCH_PARAMS_VERSION {SEARCH_PARAMS_VERSION}" in text
    diann, sage = PINNED_TOOL_VERSIONS["diann"], PINNED_TOOL_VERSIONS["sage"]
    assert relay.PINNED_DIANN_VERSION == diann.rsplit(".", 1)[0] == "2.3"
    assert "DIA-NN 2.3.x" in text and f"Pinned {diann}" in text
    assert f"Sage {sage.rsplit('.', 1)[0]}.x" in text and f"Pinned {sage}" in text
    d = COMMUNITY_DIANN_PARAMS_FROZEN
    assert f"peptides {d['min-pep-len']}–{d['max-pep-len']} residues" in text
    assert f"{d['missed-cleavages']} missed cleavage" in text
    assert f"Precursor charge {d['min-pr-charge']}–{d['max-pr-charge']}" in text
    assert d["qvalue"] == 0.01 and "q-value ≤ 0.01" in text
    e = COMMUNITY_SAGE_PARAMS["database"]["enzyme"]
    assert (e["min_len"], e["max_len"], e["missed_cleavages"], e["restrict"]) == (7, 30, 1, "P")
    assert COMMUNITY_SAGE_PARAMS["precursor_tol"] == {"ppm": [-10, 10]} and "Precursor ±10 ppm" in text.replace("&plusmn;", "±").replace("&nbsp;", " ")
    assert COMMUNITY_SAGE_PARAMS["fragment_tol"] == {"ppm": [-20, 20]}
    for name, md5 in EXPECTED_ASSET_HASHES.items():
        assert name in text and f"md5 {md5}" in text, name
    # The checksums are stamped, not computed (stan/community/submit.py), and
    # any present checksum marks a row assets-verified (normalize_v1.py).
    submit = (REPO / "stan/community/submit.py").read_text()
    assert 'EXPECTED_ASSET_HASHES.get("human_hela_202604.fasta"' in submit
    assert 'EXPECTED_ASSET_HASHES.get(\n                "hela_timstof_202604.parquet"' in submit
    norm = (REPO / "stan/community/normalize_v1.py").read_text()
    assert "ok = bool(fasta)" in norm and "ok = ok and bool(speclib)" in norm
    assert "The checksums on a row do not prove what was searched" in text
    assert "it does not hash the FASTA and library the search actually used" in text
    assert "matching FASTA and library checksums" not in text
    # An install's own library is used automatically (stan/search/local.py).
    local = (REPO / "stan/search/local.py").read_text()
    assert 'get_user_config_dir() / "instrument_library.parquet"' in local and "instrument_library.parquet" in text
    assert "although their rows carry the full library's checksum" in text
    # Only a stated version is checked by the relay.
    app = (REPO / "hf_space/app.py").read_text()
    assert "    if sub.diann_version:\n        ver_parts = sub.diann_version.split(\".\")" in app
    assert "the relay refuses a submission that states any other version; one that states no version is not checked" in text
    assert "refuses other versions" not in text
    # Coverage divides by the full library, so a subset search reads low.
    assert "Coverage divides by the full library's size, so for runs searched against a subset it reads slightly low" in text
    # The count definition is the extractor's.
    ext = (REPO / "stan/metrics/extractor.py").read_text()
    assert 'pl.col("Q.Value") <= q_cutoff' in ext and '"n_precursors": filt["Precursor.Id"].n_unique()' in ext
    assert "Unique Precursor.Id (modified sequence + charge) at run-level Q.Value ≤ 0.01" in text
    assert 'filt.filter(pl.col("PG.Q.Value") <= q_cutoff)' in ext and "PG.Q.Value ≤ 0.01" in text
    # Decision 11, said plainly; IPS hidden; the within-vendor caveat.
    assert "Not yet true for UC Davis's timsTOF HT and Exploris 480 cohorts" in text
    assert "subsets of these libraries" in text and "decision 11" in text
    assert "IPS is not shown on this page yet" in text and "recalibrat" in text
    assert "Counts compare within a vendor" in text
    assert "empirical HeLa libraries, one per vendor (timsTOF ~54k, Orbitrap ~170k precursors)" in methods


# ── page behaviour, in node ──────────────────────────────────────────

@needs_node
def test_colkey_treats_unknown_as_no_column(client, tmp_path):
    scenario = """[
      colKey({column_model: 'Unknown'}), colKey({column_model: ' unknown '}), colKey({column_model: ''}),
      colKey({}), colKey({column_model: 'PepSep MAX 10cm'}),
      columnName({column_vendor: 'Unknown', column_model: 'Unknown'}),
      columnName({column_vendor: 'PepSep', column_model: 'PepSep MAX 10cm'}),
      columnName({column_vendor: 'IonOpticks', column_model: 'Aurora 25cm'})
    ]"""
    assert _run(client, tmp_path, scenario) == [
        "", "", "", "", "pepsep max 10cm", "", "PepSep MAX 10cm", "IonOpticks Aurora 25cm"]


def _card_rows() -> list[dict]:
    rows = [_row(i, run_date=f"2026-0{1 + i % 9}-10T10:00:00Z") for i in range(12)]          # HT 100 SPD, Unknown column
    rows += [_row(20 + i, column_vendor="PepSep", column_model="PepSep MAX 10cm") for i in range(4)]
    rows += [_row(40 + i, spd=spd, cohort_id="timsTOF_60spd_low", n_precursors=45000 + i,     # HT 60 SPD tier, 46-60 SPD
                  gradient_length_min=21 if spd == 60 else 30)
             for i, spd in enumerate([46, 50, 60, 60, 46, 60])]
    ex = dict(instrument_family="Exploris", instrument_model="Orbitrap Exploris 480", cohort_id="Exploris_30spd_low",
              spd=38, lc_system="custom", gradient_length_min=44)
    rows += [_row(60 + i, **ex) for i in range(3)]                                             # sparse DIA
    rows += [_dda(70 + i, **ex) for i in range(2)]                                              # sparse DDA
    return rows


@needs_node
def test_reference_cards_grouped_by_model_with_sparse_folded(client, tmp_path):
    """D5: grouped under model headings, primary metric large, cohorts that
    cannot be ranked folded, and "Unknown" never a column.

    P2b: each card is one cohort of the page's one cohort key (B2), titled by
    its gradient ("Evosep 100 SPD", "~30 min gradient (38 SPD) · 44 min run") rather than P2a's
    "gradients seen: 46–60 SPD" over a throughput tier; an Evosep run at a
    non-Evosep SPD is "SPD unverified" and not ranked."""
    html = _run(client, tmp_path, f"allData = {json.dumps(_card_rows())}; view.mode = 'all'; renderRefRanges(); els['ref-ranges-container'].innerHTML")
    groups = html.split('<details class="mgroup"')[1:]
    assert len(groups) == 2
    ht, ex = groups
    assert ">timsTOF HT</h3>" in ht and ">Orbitrap Exploris 480</h3>" in ex
    assert "1 cohort + 4 not ranked · 22 runs · 1 lab" in ht
    cards = ht.split('<article class="rc">')[1:]
    assert len(cards) == 1
    [big] = cards
    # the cohort card holds every run, the column is named once, "Unknown" nowhere
    assert "<h4>Evosep 100 SPD</h4>" in big and "All columns combined" in big and "16 runs · 1 lab" in big
    assert '<div class="rc-big">40,750<small>median precursors</small></div>' in big
    assert "Middle half <b>" in big and "Proteins · context" in big and "single-lab reference" in big
    assert "Run length recorded: 11 min" in big
    assert "Unknown" not in html and " SPD · 26-75 ng" not in html
    # the 4-run PepSep column card, the 3-run Evosep 60 SPD cohort and the
    # unverified 46 and 50 SPD runs are folded, each with its reason
    assert "Show 4 not-ranked cohorts (10 runs)" in ht
    assert "DIA · Evosep 100 SPD · 50 ng · PepSep MAX 10cm" in ht and "not ranked: fewer than 5 runs" in ht
    assert "DIA · Evosep 60 SPD · 50 ng</b>" in ht
    assert "DIA · Evosep, 30 min run (SPD 46 unverified) · 50 ng</b>" in ht
    assert "not ranked: recorded as Evosep, but 46 SPD is not an Evosep method (SPD unverified)" in ht
    # a model with only unranked cohorts opens its fold, DIA and DDA apart
    assert "<div class=\"refgrid\">" not in ex and "Only 2 not-ranked cohorts (5 runs)" in ex
    assert "DIA · ~30 min gradient (38 SPD) · 44 min run · 50 ng</b>" in ex and "3 runs · 1 lab · precursors" in ex
    assert "DDA · ~30 min gradient (38 SPD) · 44 min run · 50 ng</b>" in ex and "2 runs · 1 lab · PSMs" in ex
    assert "IPS" not in html


@needs_node
def test_reference_cards_and_filters_escape_submitter_strings(client, tmp_path):
    """P2b: the per-panel family and mode checkboxes are the filter bar's
    Instrument and Mode now; its menus are escaped too."""
    rows = [_row(i, instrument_model=EVIL, instrument_family=EVIL, column_vendor="<b>v</b>",
                 column_model="<script>x</script>") for i in range(6)]
    scenario = f"""(() => {{ allData = {json.dumps(rows)};
        renderRefRanges(); renderFilterBar();
        return els['ref-ranges-container'].innerHTML + els['fbar-model'].innerHTML + els['fbar-column'].innerHTML; }})()"""
    html = _run(client, tmp_path, scenario)
    assert "<img" not in html and "<script>" not in html and "<b>v</b>" not in html
    assert "&lt;img" in html and "&lt;script&gt;" in html


@needs_node
def test_dedupe_keeps_one_copy_of_each_acquisition(client, tmp_path):
    """D8, the mockup's key: same instrument, track and all four counts, run
    dates within 2 s of each other; the larger contributor's copy is kept."""
    base = dict(n_precursors=41000, n_peptides=36000, n_proteins=5000, n_psms=0)
    t = "2026-09-20T10:00:{:02d}.{:03d}123+00:00"
    rows = [
        _row(1, display_name="Big Lab", run_date=t.format(0, 0), submitted_at="2026-09-21T00:00:02Z", **base),
        _row(2, display_name="Small Lab", run_date=t.format(1, 500), submitted_at="2026-09-21T00:00:01Z", **base),
        _row(3, display_name="Big Lab", run_date=t.format(10, 0), **base),                 # 8.5 s later: its own run
        _row(4, display_name="Big Lab", run_date=t.format(10, 0), acquisition_mode="dda", **base),  # other track
        _row(5, display_name="Big Lab", run_date=t.format(10, 0), instrument_model="timsTOF Pro", **base),
        _row(6, display_name="Big Lab", run_date=None, **base), _row(7, display_name="Big Lab", run_date=None, **base),
        _row(8, display_name="Big Lab", run_date=t.format(30, 0), **dict(base, n_precursors=50000)),
        _row(9, display_name="Big Lab", run_date=t.format(31, 500), **dict(base, n_precursors=50000)),   # chain: 1.5 s
        _row(10, display_name="Big Lab", run_date=t.format(33, 0), **dict(base, n_precursors=50000)),    # 1.5 s after the last
    ]
    got = _run(client, tmp_path, f"(() => {{ const d = dedupeRuns({json.dumps(rows)}); return [d.dropped, d.kept.map(s => s.submission_id)]; }})()")
    dropped, kept = got
    assert dropped == 3
    assert kept == ["s1", "s3", "s4", "s5", "s6", "s7", "s8"]   # original order kept


@needs_node
def test_held_back_amounts_leave_every_panel_and_are_counted(client, tmp_path):
    rows = [_row(i) for i in range(4)] + [_row(10, amount_ng=5000), _row(11, amount_ng=5001), _row(12, amount_ng=562100)]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(rows)}); updateStats();
        return [allData.map(s => s.submission_id), els['stat-submissions'].textContent, els['stats-note'].textContent];
    }})()"""
    ids, tile, note = _run(client, tmp_path, scenario)
    assert ids == ["s0", "s1", "s2", "s3", "s10"] and tile == "5"
    assert note.startswith("Built from 7 submitted rows:")
    assert "2 runs held back from every range and ranking because the stored amount is above 5,000 ng" in note


def _best_rows() -> list[dict]:
    rows = [_row(i, n_precursors=40000 + i) for i in range(5)]
    rows += [_row(10 + i, instrument_family="Lumos", instrument_model="Orbitrap Fusion Lumos", spd=9,
                  lc_system="custom", gradient_length_min=128,
                  cohort_id="Lumos_deep_very-high", amount_ng=1000, n_precursors=80000 + i) for i in range(5)]
    return rows


@needs_node
def test_best_configurations_amount_select_defaults_to_50_ng(client, tmp_path):
    """B6: a >=250 ng cohort is not ranked against 50 ng ones unless asked.

    P2b: the amount is the filter bar's; the select on the card is a view of it."""
    scenario = f"""(() => {{
        allData = {json.dumps(_best_rows())}; view.mode = 'dia';
        renderConfigLeaderboard();
        const first = [els['config-leaderboard'].innerHTML, els['config-leaderboard-badge'].textContent];
        setView({{ amount: 'all' }});
        return first.concat([els['config-leaderboard'].innerHTML, els['config-leaderboard-badge'].textContent,
                             els['config-amount-filter'].value]);
    }})()"""
    html50, badge50, html_all, badge_all, mirror = _run(client, tmp_path, scenario)
    models = lambda h: re.findall(r'font-weight:600">([^<]+)</span>', h)  # noqa: E731
    assert models(html50) == ["timsTOF HT"] and badge50 == "HeLa · DIA · 50 ng · sorted by precursors"
    assert models(html_all) == ["Orbitrap Fusion Lumos", "timsTOF HT"] and badge_all == "HeLa · DIA · all amounts · sorted by precursors"
    assert mirror == "all"
    # the primary metric is the column right after Instrument (phone order)
    heads = re.findall(r"<th[^>]*>([^<]+)<", html50)
    assert heads[1:4] == ["Instrument", "Precursors ▼", "LC and gradient"]


@needs_node
def test_violins_one_per_spd_cohort_and_track(client, tmp_path):
    rows = [_row(i) for i in range(6)]                                                         # HT 100 SPD
    rows += [_row(10 + i, spd=60, cohort_id="timsTOF_60spd_low") for i in range(6)]            # HT 60 SPD
    rows += [_row(20 + i, spd=30, cohort_id="timsTOF_30spd_low") for i in range(2)]            # too few
    rows += [_dda(30 + i) for i in range(5)]                                                   # HT 100 SPD DDA
    scenario = f"""(() => {{
        allData = {json.dumps(rows)};
        const out = {{}};
        for (const [tab, width] of [['all', 1280], ['dia', 1280], ['all', 200]]) {{
            view.mode = tab; window.innerWidth = width; renderViolin();
            const p = plots[plots.length - 1], v = p.traces.filter(t => t.type === 'violin');
            const ax = v[0].orientation === 'h' ? p.layout.yaxis : p.layout.xaxis;
            out[tab + width] = {{ n: v.length, orient: v[0].orientation, ticks: ax.ticktext,
                                  note: els['violin-note'].textContent }};
        }}
        return out;
    }})()"""
    got = _run(client, tmp_path, scenario)
    assert got["all1280"]["n"] == 3 and got["all1280"]["orient"] == "v"
    # P2b: one violin per cohort of the page's cohort key, named by its Evosep method
    assert [t for t in got["all1280"]["ticks"] if "DDA" in t] == ["timsTOF HT<br>Evosep<br>100 SPD<br>DDA"]
    assert got["all1280"]["note"].startswith("1 cohort not ranked (2 runs: fewer than 5 runs")
    assert got["dia1280"]["n"] == 2 and got["dia1280"]["ticks"][:2] == ["timsTOF HT<br>Evosep<br>60 SPD", "timsTOF HT<br>Evosep<br>100 SPD"]
    # under 70 px a violin (phones): horizontal, so the labels no longer clip; a vendor label row heads the group
    assert got["all200"]["orient"] == "h"
    assert "timsTOF HT · Evosep 100 SPD · DDA" in got["all200"]["ticks"]
    assert any("~54k-precursor library" in t for t in got["all200"]["ticks"])


@needs_node
def test_depth_by_throughput_facets_per_model(client, tmp_path):
    rows = [_row(i) for i in range(4)]
    rows += [_row(10 + i, instrument_model="timsTOF Pro") for i in range(3)]
    rows += [_row(20 + i, instrument_model="timsTOF Pro 2") for i in range(2)]
    rows += [_dda(30 + i) for i in range(3)]
    scenario = f"""(() => {{
        allData = {json.dumps(rows)}; view.mode = 'dia'; renderSpdDepth();
        const dia = plots[plots.length - 1];
        view.mode = 'all'; renderSpdDepth();
        const all = plots[plots.length - 1];
        const heads = p => p.layout.annotations.map(a => a.text.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim());
        return [heads(dia), heads(all), dia.layout.yaxis.title];
    }})()"""
    dia, all_, ytitle = _run(client, tmp_path, scenario)
    assert dia == ["timsTOF HT 4 runs · 1 lab, 50 ng", "timsTOF Pro 3 runs · 1 lab, 50 ng",
                   "timsTOF Pro 2 2 runs · 1 lab, 50 ng"]
    assert ytitle == "Precursors"
    assert "timsTOF HT · DDA 3 runs · 1 lab, 50 ng" in all_ and "timsTOF HT · DIA 4 runs · 1 lab, 50 ng" in all_


@needs_node
def test_column_comparison_stays_visible_with_an_honest_empty_state(client, tmp_path):
    rows = [_row(i) for i in range(8)] + [_row(10 + i, column_vendor="PepSep", column_model="PepSep MAX 10cm") for i in range(3)]
    two = rows + [_row(20 + i, column_vendor="IonOpticks", column_model="Aurora 25cm") for i in range(3)]
    scenario = f"""(() => {{
        view.mode = 'dia';
        allData = {json.dumps(rows)}; renderColumnComparison();
        const one = {{ note: els['column-compare-note'].innerHTML, row: els['row-column-compare'].style.display,
                       traces: plots[plots.length - 1].traces }};
        allData = {json.dumps(two)}; renderColumnComparison();
        return [one, {{ note: els['column-compare-note'].innerHTML, traces: plots[plots.length - 1].traces }}];
    }})()"""
    one, two_ = _run(client, tmp_path, scenario)
    assert "needs a second known column in one cohort" in one["note"] and one["row"] == ""
    assert "8 of 11 runs in view record no column and are left out" in one["note"]
    assert [t["name"] for t in one["traces"]] == ["PepSep MAX 10cm"]
    assert two_["note"] == ""
    assert sorted(t["name"] for t in two_["traces"]) == ["IonOpticks Aurora 25cm", "PepSep MAX 10cm"]
    for t in two_["traces"]:
        assert t["text"] == ["3 runs · 1 lab<br>Sep 2026"]          # runs, labs and date span on each bar
        assert t["x"] == ["timsTOF HT · DIA<br>Evosep 100 SPD · 50 ng"]   # P2b: the gradient's name (B2)
    assert "Unknown" not in json.dumps(one) + json.dumps(two_)


@needs_node
def test_id_free_charts_draw_one_line_per_model(client, tmp_path):
    rows = [_row(i, run_date=f"2026-0{1 + i % 3}-10T10:00:00Z") for i in range(6)]
    rows += [_row(10 + i, instrument_model="timsTOF Pro", run_date=f"2026-0{1 + i % 3}-12T10:00:00Z") for i in range(3)]
    scenario = f"""(() => {{ allData = {json.dumps(rows)}; renderMs1Signal();
        return plots[plots.length - 1].traces.map(t => [t.mode, t.name, t.showlegend === false, (t.x || []).length]); }})()"""
    traces = _run(client, tmp_path, scenario)
    # points and lines carry no legend entry; each model has one solid key
    assert traces == [
        ["markers", "timsTOF HT runs", True, 6],
        ["markers", "timsTOF Pro runs", True, 3],
        ["lines", "timsTOF HT monthly median", True, 3],
        ["lines", "timsTOF Pro monthly median", True, 3],
        ["lines+markers", "timsTOF HT (6 runs · 1 lab)", False, 1],
        ["lines+markers", "timsTOF Pro (3 runs · 1 lab)", False, 1],
    ]


@needs_node
def test_depth_by_amount_states_the_50_ng_share_not_saturation(client, tmp_path):
    html = _page(client)
    assert "Saturation typically" not in html and 'id="amount-share"' in html
    rows = [_row(i) for i in range(9)] + [_row(20, amount_ng=200)]
    got = _run(client, tmp_path, f"allData = {json.dumps(rows)}; view.mode = 'dia'; renderAmountDepth(); els['amount-share'].textContent")
    assert got.startswith("90% of the 10 runs in view are at 50 ng")


@needs_node
def test_published_field_list_is_what_the_api_serves(client, tmp_path):
    rows = [dict(_row(1), _tic={"rt": []}), _row(2, lc_system="evosep")]
    for r in rows:
        r.pop("run_name")                     # /api/leaderboard never serves it (P1 test)
    scenario = f"""(() => {{ allDataRaw = {json.dumps(rows)}; renderPublishedFields();
        return [publishedFields(allDataRaw), els['fl-sum'].textContent, els['fl-list'].innerHTML]; }})()"""
    fields, summary, html = _run(client, tmp_path, scenario)
    assert "tic_rt_bins" in fields and "tic_intensity" in fields and "lc_system" in fields
    assert "_tic" not in fields and fields == sorted(fields)
    assert summary == f"Every published field, by API name ({len(fields)})"
    assert html.count("<code>") == len(fields)


# ── Review fixes before the P2a deploy (2026-09-30) ──────────────────

_SAME = dict(n_precursors=41000, n_peptides=36000, n_proteins=5000, n_psms=0, run_date="2026-09-20T10:00:00+00:00")


def _big_lab(n: int = 5) -> list[dict]:
    return [_row(900 + i, display_name="Big Lab") for i in range(n)]


@needs_node
def test_dedupe_prefers_a_usable_copy_and_inherits_only_the_column(client, tmp_path):
    """A held-back or flagged copy that won the tie-break was then filtered
    out, losing the acquisition. Preferring the copy that records a column
    swapped in whole older seed rows (identified-ion TIC, no lc_system, other
    SPD and amount), so the kept copy is chosen without looking at the column
    and takes only column_vendor/column_model from a dropped copy."""
    raw_tic = json.dumps([round(0.05 + 0.1 * j, 3) for j in range(30)])
    idion_tic = json.dumps([round(2.0 + 0.1 * j, 3) for j in range(30)])
    ys = json.dumps([float(1 + j % 10) for j in range(30)])
    newer = dict(_SAME, display_name="Big Lab", lc_system="evosep", spd=60, amount_ng=50, stan_version="0.2.376",
                 tic_rt_bins=raw_tic, tic_intensity=ys, submitted_at="2026-05-28T00:00:00Z")
    seed = dict(_SAME, display_name="Small Lab", lc_system="", spd=100, amount_ng=40, stan_version="0.2.282",
                tic_rt_bins=idion_tic, tic_intensity=ys, column_vendor="PepSep", column_model="PepSep MAX 10cm",
                submitted_at="2026-04-01T00:00:00Z")
    cases = {
        "inherit": [_row(1, **newer), _row(2, **seed)],
        "held": [_row(1, **dict(newer, amount_ng=50000)), _row(2, **seed)],
        "flagged": [_row(1, is_flagged=True, **newer), _row(2, **seed)],
        "usable_donor": [_row(1, **newer), _row(2, is_flagged=True, **dict(seed, column_model="Flagged Col")),
                         _row(3, **dict(seed, display_name="Other Lab", column_model="Usable Col"))],
        "own_column": [_row(1, **dict(newer, column_vendor="IonOpticks", column_model="Aurora 25cm")), _row(2, **seed)],
    }
    scenario = "(() => { const out = {};" + "".join(f"""
        {{ const src = {json.dumps(v + _big_lab())}; const before = JSON.stringify(src);
          const r = dedupeRuns(src); const k = r.kept.find(s => s.n_precursors === 41000);
          const orig = src.find(s => s.submission_id === k.submission_id);
          out.{name} = {{ dropped: r.dropped, id: k.submission_id, lc: k.lc_system, spd: k.spd, amount: k.amount_ng,
                          version: k.stan_version, idion: ticOf({{...k}}).idion, col: [k.column_vendor, k.column_model],
                          untouched: JSON.stringify(src) === before, same: k === orig }}; }}""" for name, v in cases.items()) + "return out; })()"
    got = _run(client, tmp_path, scenario)
    # the newer copy stays whole, and takes only the seed copy's column
    assert got["inherit"] == {"dropped": 1, "id": "s1", "lc": "evosep", "spd": 60, "amount": 50, "version": "0.2.376",
                              "idion": False, "col": ["PepSep", "PepSep MAX 10cm"], "untouched": True, "same": False}
    # a held-back or flagged copy still loses, whatever its lab's size
    assert got["held"]["id"] == "s2" and got["flagged"]["id"] == "s2"
    assert got["held"]["col"] == ["PepSep", "PepSep MAX 10cm"] and got["held"]["same"] is True
    # the column comes from a usable dropped copy before a flagged one
    assert got["usable_donor"]["id"] == "s1" and got["usable_donor"]["col"] == ["PepSep", "Usable Col"]
    # a kept copy with its own column keeps it, and is the source object itself
    assert got["own_column"]["col"] == ["IonOpticks", "Aurora 25cm"] and got["own_column"]["same"] is True
    # the fetched rows are never changed in place
    assert all(c["untouched"] for c in got.values())


@needs_node
def test_a_cohort_split_across_columns_keeps_its_own_card(client, tmp_path):
    """3 + 3 runs on two columns: each column card is sparse, but the 6-run
    cohort still gets its "All columns combined" card."""
    rows = [_row(i, column_vendor="PepSep", column_model="PepSep MAX 10cm") for i in range(3)]
    rows += [_row(10 + i, column_vendor="IonOpticks", column_model="Aurora 25cm") for i in range(3)]
    html = _run(client, tmp_path, f"allData = {json.dumps(rows)}; renderRefRanges(); els['ref-ranges-container'].innerHTML")
    cards = html.split('<article class="rc">')[1:]
    assert len(cards) == 1 and "All columns combined" in cards[0] and "6 runs · 1 lab" in cards[0]
    assert "Show 2 not-ranked cohorts" in html


@needs_node
def test_column_colours_do_not_depend_on_the_tab(client, tmp_path):
    rows = [_row(i, column_vendor="PepSep", column_model="PepSep MAX 10cm") for i in range(3)]
    rows += [_row(10 + i, column_vendor="IonOpticks", column_model="Aurora 25cm") for i in range(3)]
    rows += [_dda(20 + i, column_vendor="IonOpticks", column_model="Aurora 25cm") for i in range(3)]
    scenario = f"""(() => {{
        allDataRaw = allData = {json.dumps(rows)};
        const colours = (tab) => {{ view.mode = tab; renderColumnComparison();
            return Object.fromEntries(plots[plots.length - 1].traces.map(t => [t.name, t.marker.color])); }};
        return [colours('dia'), colours('dda')];
    }})()"""
    dia, dda = _run(client, tmp_path, scenario)
    assert dda["IonOpticks Aurora 25cm"] == dia["IonOpticks Aurora 25cm"]
    assert dia["IonOpticks Aurora 25cm"] != dia["PepSep MAX 10cm"]


@needs_node
def test_edge_case_wording(client, tmp_path):
    rows = [_dda(i) for i in range(3)]
    labelled = [_row(i, column_vendor="PepSep", column_model="PepSep MAX 10cm") for i in range(3)]
    scenario = f"""(() => {{
        view.mode = 'dia'; allData = {json.dumps(rows)}; renderColumnComparison();
        const ddaOnly = els['column-compare-note'].innerHTML;
        allData = {json.dumps(labelled)}; renderColumnComparison();
        const allLabelled = els['column-compare-note'].innerHTML;
        setSubmissions({json.dumps([_row(1)])}); updateStats();
        const one = els['stats-note'].textContent;
        setSubmissions([]); renderPublishedFields();
        return [ddaOnly, allLabelled, one, els['fl-sum'].textContent, els['fl-list'].innerHTML];
    }})()"""
    dda_only, all_labelled, one, fl_sum, fl_list = _run(client, tmp_path, scenario)
    assert dda_only == "<b>Nothing to compare yet.</b> There are no DIA runs in view."
    assert "record no column" not in all_labelled and "0 of" not in all_labelled
    assert one == "Built from the 1 submitted row: no duplicate copies or implausible amounts found."
    assert fl_sum == "Every published field, by API name" and "<code>" not in fl_list


@needs_node
def test_points_across_peak_legend_is_solid_and_the_note_is_clear_of_the_data(client, tmp_path):
    rows = [_row(i, instrument_family="Exploris", instrument_model="Orbitrap Exploris 480") for i in range(3)]
    rows += [_row(10 + i, column_vendor="PepSep", column_model="PepSep MAX 10cm") for i in range(2)]
    scenario = f"""(() => {{ allData = {json.dumps(rows)}; renderPointsAcrossPeak();
        const p = plots[plots.length - 1];
        return [p.traces.filter(t => t.showlegend !== false).map(t => [t.name, (t.marker || {{}}).symbol || null]),
                p.layout.annotations[0].y, p.layout.annotations[0].yanchor]; }})()"""
    keys, y, yanchor = _run(client, tmp_path, scenario)
    assert ["Orbitrap Exploris 480 (3 runs · 1 lab)", "circle"] in keys
    assert ["timsTOF HT (2 runs · 1 lab)", "circle"] in keys
    assert ["Column not recorded", "circle-open"] in keys and ["PepSep column", "square"] in keys
    assert y > 1 and yanchor == "bottom"      # above the plot, not on the runs


@needs_node
def test_undated_rows_do_not_set_the_latest_run(client, tmp_path):
    rows = [_row(1, run_date="2026-03-05T10:00:00Z"), _row(2, run_date=None, submitted_at=None)]
    scenario = f"""(() => {{ setSubmissions({json.dumps(rows)}); updateStats();
        return [els['stat-latest'].textContent, els['stat-first'].textContent, dateSpanText(allData)]; }})()"""
    assert _run(client, tmp_path, scenario) == ["Mar 5, 2026", "first run Mar 2026", "Mar 2026"]


@needs_node
def test_family_filter_follows_the_qc_standard_and_keeps_unticks(client, tmp_path):
    """Switching to Yeast used to leave a yeast-only family unticked, so its
    cards read "No data yet".

    P2b: the family checkboxes are the filter bar's Instrument. Picking one
    narrows the cards to it; a QC standard without that instrument falls back
    to every instrument instead of showing nothing."""
    hela = [_row(i) for i in range(6)] + [_row(10 + i, instrument_family="Lumos", instrument_model="Orbitrap Fusion Lumos") for i in range(6)]
    yeast = [_row(20 + i, instrument_family="Astral", instrument_model="Orbitrap Astral", sample_type="yeast") for i in range(6)]
    scenario = f"""(() => {{
        setSubmissions({json.dumps(hela + yeast)}); renderRefRanges();
        setView({{ model: 'timsTOF HT' }});                          // the reader picks timsTOF HT
        const picked = els['ref-ranges-container'].innerHTML;
        setView({{ sample: 'yeast' }});                              // QC standard: yeast
        const yeastHtml = els['ref-ranges-container'].innerHTML, yeastModel = view.model;
        setView({{ sample: 'hela' }});                               // back to HeLa
        return [picked, yeastHtml, yeastModel, els['ref-ranges-container'].innerHTML];
    }})()"""
    picked, yeast_html, yeast_model, back = _run(client, tmp_path, scenario)
    assert "Orbitrap Fusion Lumos" not in picked and "timsTOF HT" in picked
    assert "Orbitrap Astral" in yeast_html and "No data yet" not in yeast_html and yeast_model == ""
    assert "Orbitrap Fusion Lumos" in back and "timsTOF HT" in back


@needs_node
def test_horizontal_violins_keep_a_short_value_title(client, tmp_path):
    rows = [_row(i) for i in range(6)] + [_row(10 + i, spd=60, cohort_id="timsTOF_60spd_low") for i in range(6)]
    rows += [_dda(20 + i) for i in range(5)]
    scenario = f"""(() => {{ allData = {json.dumps(rows)}; view.mode = 'all';
        window.innerWidth = 200; renderViolin(); const h = plots[plots.length - 1].layout.xaxis.title;
        window.innerWidth = 1280; renderViolin(); const v = plots[plots.length - 1].layout.yaxis.title;
        return [h, v]; }})()"""
    assert _run(client, tmp_path, scenario) == ["Precursors / PSMs", "Precursors (DIA) / PSMs (DDA)"]


def test_explorer_intro_and_dead_css(client):
    html = _page(client)
    intro = _section(html, "explore")
    intro = re.sub(r"\s+", " ", intro[:intro.index('<div class="chart-row">')])
    # P2b: every chart follows the filter bar and says so in its badge
    assert "Every chart here follows the filter bar at the top of the page and says in its badge what it shows" in intro
    assert "The TIC overlay keeps its own SPD, LC and acquisition-mode menus and follows only the QC standard" in intro
    assert "Throughput vs. Quantitation Quality shows every run" not in intro
    css = html[:html.index("</style>")]
    for dead in (".ref-card", ".ref-row", ".ref-grid", ".ref-metric", ".ref-range", ".ref-n", ".ref-vals"):
        assert dead not in css, dead


@needs_node
def test_library_coverage_caveat_says_subset_runs_read_low(client, tmp_path):
    rows = [_row(i, library_coverage_pct=60.0 + i) for i in range(5)] + [_row(9, library_coverage_pct=92.0)]
    got = _run(client, tmp_path, f"(() => {{ setSubmissions({json.dumps(rows)}); renderLibraryCaveat(); return els['lib-caveat'].textContent; }})()")
    assert got.startswith("timsTOF runs cover a median 63% of their library (highest 92%)")
    assert "1 run is there today" in got
    assert "Coverage divides by the full library's 54,000 precursors" in got and "it reads slightly low" in got
