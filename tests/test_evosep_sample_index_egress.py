"""The 30-minute Evosep tick must not download the whole injection history.

WHY THIS EXISTS. `cron_evosep.sh` runs `extract_evosep.py --since <3 days
ago>` every half hour, and until 2026-09-22 `load_sample_index` ignored the
window: every tick read every timsTOF `sample_health` row -- 2,064 rows,
143 KB -- to attribute pressure steps in three days of Evosep runs. PG Farm
bills every byte it serves, so that was ~7 MB a day spent on rows the tick
could never join to anything. Scoped to the window it is 19 rows, 1.2 KB.

WHY A STRING COMPARISON IS RIGHT. `sample_health.run_date` is TEXT, and all
3,854 rows are ISO-8601 with a 'T' (verified live 2026-09-22), e.g.
`2026-09-21T20:13:45.944-07:00`. ISO strings sort chronologically, and a
longer string with the same date prefix sorts after the bare date, so
`run_date >= '2026-09-18'` keeps every row from that date on.

WHY A DAY OF MARGIN. `--since` is a date on the Evosep's clock (naive local
time from its folder names), and the stored run_date is local-with-offset on
the same clock. The attribution window reaches ATTRIB_TOL_MIN (25 min) either
side of a procedure's start, so the window's first procedure can pair with an
acquisition logged just before midnight the previous day. One day covers that
with room to spare and costs a few dozen rows.

The full extract (no `--since`) still reads everything: it attributes steps
across the whole column history, so it needs all of it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "instrument" / "evosep"))

import extract_evosep as ex  # noqa: E402


class FakePG:
    """psycopg2 connection + cursor that answers from a list of rows.

    It applies the `run_date >= %s` floor the extractor sends as a plain
    Python string comparison -- the same comparison PG makes on a TEXT column
    -- so the tests see what the tick would actually receive.
    """

    def __init__(self, rows: list[tuple[str, str, str]]):
        self.rows = rows  # (instrument, run_name, run_date)
        self.statements: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        params = tuple(params or ())
        self.statements.append((s, params))
        assert s.count("%s") == len(params), (s, params)
        instrument, *rest = params
        out = [r for r in self.rows if r[0] == instrument]
        if "run_date >= %s" in s:
            floor = rest[0]
            out = [r for r in out if r[2] >= floor]
        self._out = [(r[1], r[2]) for r in sorted(out, key=lambda r: r[2], reverse=True)]

    def fetchall(self):
        return self._out


@pytest.fixture()
def fake_pg(monkeypatch):
    """load_sample_index imports _connect at call time; patch the module attr."""
    import stan.db_pg as db_pg

    def install(rows):
        pg = FakePG(rows)
        monkeypatch.setattr(db_pg, "_connect", lambda: pg)
        return pg

    return install


# Real-shaped names: SAMPLE_WELL_RE wants `_S<n>-<row><col>_`.
def _row(when: str, well: str = "S1-A1", sub: str = "793") -> tuple[str, str, str]:
    return ("timsTOF HT", f"20260918_{sub}_hela_{well}_1.d", when)


HISTORY = [
    _row("2025-11-02T09:00:00-08:00", "S2-B2"),
    _row("2026-09-10T10:00:00-07:00", "S2-B3"),
    _row("2026-09-17T23:30:00-07:00", "S2-B4"),   # before the floor
    _row("2026-09-18T00:01:00-07:00", "S1-A1"),   # on the floor
    _row("2026-09-19T00:08:00-07:00", "S1-A2"),   # just after --since midnight
    _row("2026-09-21T20:13:45.944-07:00", "S1-A3"),
]


def test_since_scopes_the_query_with_a_day_of_margin(fake_pg):
    pg = fake_pg(HISTORY)
    ex.load_sample_index("timsTOF HT", since="2026-09-19")

    ((sql, params),) = pg.statements
    assert "run_date >= %s" in sql
    assert params == ("timsTOF HT", "2026-09-18")


def test_since_returns_only_the_window_plus_margin(fake_pg):
    fake_pg(HISTORY)
    got = ex.load_sample_index("timsTOF HT", since="2026-09-19")
    assert {r["well"] for r in got} == {"S1-A1", "S1-A2", "S1-A3"}


def test_an_acquisition_dated_before_since_is_still_attributed(fake_pg):
    """The margin is what keeps the window's first procedure attributable.

    attribute_run matches within ATTRIB_TOL_MIN EITHER side of a procedure's
    start, so a procedure at 00:05 on the --since day can pair with an
    acquisition logged at 23:50 the day before. A floor of exactly --since
    would drop that row and the step would go unattributed.
    """
    fake_pg([("timsTOF HT", "20260918_793_hela_S1-A2_1.d", "2026-09-18T23:50:00")])
    index = ex.load_sample_index("timsTOF HT", since="2026-09-19")
    who = ex.attribute_run({"well": "S1-A2", "start": "2026-09-19T00:05:00"}, index)
    assert who and who["run_name"].startswith("20260918_793")


def test_no_since_keeps_the_unbounded_read_the_full_extract_needs(fake_pg):
    pg = fake_pg(HISTORY)
    got = ex.load_sample_index("timsTOF HT")

    ((sql, params),) = pg.statements
    assert "run_date >=" not in sql and params == ("timsTOF HT",)
    assert len(got) == len(HISTORY)


@pytest.mark.parametrize("bad", ["19/09/2026", "yesterday", "2026-13-40"])
def test_unparseable_since_falls_back_to_unbounded_not_to_nothing(fake_pg, bad):
    """Egress is a cost; losing attribution is a wrong answer. Pick the cost."""
    pg = fake_pg(HISTORY)
    got = ex.load_sample_index("timsTOF HT", since=bad)
    ((sql, params),) = pg.statements
    assert "run_date >=" not in sql
    assert len(got) == len(HISTORY)


def test_no_instrument_means_no_query(fake_pg):
    pg = fake_pg(HISTORY)
    assert ex.load_sample_index(None, since="2026-09-19") == []
    assert pg.statements == []


# ── The call site: main() must hand its --since through ──────────────────

def _write_run(root: Path, name: str, bars: float) -> None:
    d = root / "TIMS-10878_20260921_120000" / "S00230" / name
    d.mkdir(parents=True)
    lines = ["time\tPump HP:Pressure [bar]"]
    for s in range(0, 600, 10):
        lines.append(f"00:{s // 60:02d}:{s % 60:02d}.000\t{bars + (s % 30) / 10:.1f}")
    (d / f"{ex.COLUMN_PUMP}_Pressure.txt").write_text("\n".join(lines) + "\n")


@pytest.fixture()
def mirror(tmp_path):
    root = tmp_path / "evosep_logs"
    for day in (17, 18, 19, 20, 21):
        for h in (8, 12, 16):
            _write_run(root, f"100-samples-per-day_2026-09-{day}_{h:02d}-00-00", 300.0)
    return root


def _run_main(monkeypatch, tmp_path, mirror, extra: list[str]) -> list:
    seen: list = []

    def spy(instrument, since=None):
        seen.append((instrument, since))
        return []

    monkeypatch.setattr(ex, "load_sample_index", spy)
    monkeypatch.setattr(ex, "load_column_events", lambda instrument: [])
    out = tmp_path / "out.json"
    rc = ex.main(["--root", str(mirror), "--instrument", "timsTOF HT",
                  "--bruker-json", str(tmp_path / "absent.json"),
                  "--columns-yml", str(tmp_path / "absent.yml"),
                  "--max-doc-mb", "0", "--out", str(out), *extra])
    assert rc == 0 and json.loads(out.read_text())
    return seen


def test_main_passes_since_to_the_sample_index(monkeypatch, tmp_path, mirror):
    seen = _run_main(monkeypatch, tmp_path, mirror, ["--since", "2026-09-19"])
    assert seen == [("timsTOF HT", "2026-09-19")]


def test_full_extract_passes_no_since(monkeypatch, tmp_path, mirror):
    seen = _run_main(monkeypatch, tmp_path, mirror, [])
    assert seen == [("timsTOF HT", None)]
