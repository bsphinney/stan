"""Community redesign P3a, relay side (Space 1.7.0).

/api/submit accepts four optional cohort attributes (spec §A.5 B4 and
decision 12) and stores them: lc_model (cleaned, at most 80 characters),
lc_flow (nano | capillary | micro), amount_source (declared | parsed |
assumed) and faims (bool, null when unknown). An unknown value is stored as
'' / null; it never rejects a run. A client that does not send them is
accepted exactly as before, the v1 completeness gate does not ask for them,
and /api/update may patch them under the same cleaning. The page does not
read them yet (P3b/P3d), so nothing on it changes: the TIC, lookup and P2
hash pins in the other relay tests stay as they were.

Fakes come from tests/test_relay_peg.py: nothing here reaches the network.
"""

from __future__ import annotations

import io

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.test_relay_peg import SUBMIT_PAYLOAD, client, drain, hub, queued, relay  # noqa: F401  (fixtures)

P3A = ("lc_model", "lc_flow", "amount_source", "faims")


def _submit(client, relay, **extra):
    drain(relay)
    r = client.post("/api/submit", json={**SUBMIT_PAYLOAD, **extra})
    assert r.status_code == 200, r.text
    (item,) = queued(relay)
    drain(relay)
    return pq.read_table(io.BytesIO(item.data))


def _p3a(table) -> dict:
    (row,) = table.select(list(P3A)).to_pylist()
    return row


def test_accepted_without_the_fields_and_stored_as_not_recorded(client, relay):
    table = _submit(client, relay)
    assert _p3a(table) == {"lc_model": "", "lc_flow": "", "amount_source": "", "faims": None}


def test_accepted_with_the_fields_and_stored(client, relay):
    table = _submit(client, relay, lc_model="UltiMate 3000", lc_flow="nano",
                    amount_source="parsed", faims=True)
    assert _p3a(table) == {"lc_model": "UltiMate 3000", "lc_flow": "nano",
                           "amount_source": "parsed", "faims": True}


def test_parquet_types(client, relay):
    schema = _submit(client, relay, faims=False).schema
    assert schema.field("lc_model").type == pa.string()
    assert schema.field("lc_flow").type == pa.string()
    assert schema.field("amount_source").type == pa.string()
    assert schema.field("faims").type == pa.bool_()
    # Appended after every column the 1.6.0 relay wrote, which are unchanged.
    assert schema.names[-4:] == list(P3A)


@pytest.mark.parametrize("sent,stored", [
    ({"lc_flow": "turbo", "amount_source": "guessed", "faims": "maybe"},
     {"lc_flow": "", "amount_source": "", "faims": None}),
    ({"lc_flow": " Capillary ", "amount_source": "DECLARED", "faims": 0},
     {"lc_flow": "capillary", "amount_source": "declared", "faims": False}),
    ({"lc_flow": 5, "amount_source": None, "faims": 2},
     {"lc_flow": "", "amount_source": "", "faims": None}),
    ({"faims": "true"}, {"faims": True}),
])
def test_unknown_enum_values_are_cleaned_not_rejected(client, relay, sent, stored):
    row = _p3a(_submit(client, relay, **sent))
    for k, v in stored.items():
        assert row[k] == v, (k, row[k])


def test_lc_model_is_cleaned_and_capped_at_80(client, relay):
    row = _p3a(_submit(client, relay, lc_model="  Vanquish​  Neo‮ " + "x" * 200))
    assert row["lc_model"].startswith("Vanquish Neo ")
    assert len(row["lc_model"]) == 80
    assert "​" not in row["lc_model"] and "‮" not in row["lc_model"]
    assert _p3a(_submit(client, relay, lc_model=42))["lc_model"] == ""


def test_v1_gate_does_not_ask_for_them(relay):
    for required in (relay.V1_REQUIRED_DIA_STR, relay.V1_REQUIRED_DIA_NUM,
                     relay.V1_REQUIRED_DIA_LIST, relay.V1_REQUIRED_DDA_STR,
                     relay.V1_REQUIRED_DDA_NUM):
        assert not set(P3A) & set(required)


V1_DDA = {
    **SUBMIT_PAYLOAD,
    "schema_version": "v1.0.0", "fasta_md5": "f" * 32,
    "column_vendor": "PepSep", "column_model": "PepSep 15cm",
    "ips_score": 60,
}


def test_v1_submission_without_the_fields_is_still_accepted(client, relay):
    drain(relay)
    r = client.post("/api/submit", json=V1_DDA)
    assert r.status_code == 200, r.text
    drain(relay)


def test_v1_gate_still_rejects_what_it_rejected(client, relay):
    drain(relay)
    r = client.post("/api/submit", json={**V1_DDA, "column_model": "", "lc_flow": "nano"})
    assert r.status_code == 422
    assert "Missing: column_model" in r.json()["detail"]
    assert queued(relay) == []


def test_update_may_patch_them_under_the_same_cleaning(client, relay, hub, monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    table = _submit(client, relay)
    sid = table.column("submission_id")[0].as_py()
    buf = io.BytesIO()
    pq.write_table(table, buf)
    hub.files[f"submissions/{sid}.parquet"] = buf.getvalue()

    r = client.post(f"/api/update/{sid}", json={
        "lc_model": "Vanquish Neo", "lc_flow": " Nano ", "amount_source": "bogus", "faims": 1})
    assert r.status_code == 200, r.text
    patched = pq.read_table(io.BytesIO(hub.files[f"submissions/{sid}.parquet"]))
    assert _p3a(patched) == {"lc_model": "Vanquish Neo", "lc_flow": "nano",
                             "amount_source": "", "faims": True}


def test_leaderboard_serves_them_and_old_rows_read_null(client, hub):
    """benchmark_latest mixes rows from before and after 1.7.0."""
    import polars as pl

    old = {"submission_id": "a", "display_name": "Lab", "instrument_model": "timsTOF HT",
           "acquisition_mode": "dia", "n_precursors": 1, "run_name": "x.d"}
    new = {**old, "submission_id": "b", "lc_model": "Evosep One", "lc_flow": "",
           "amount_source": "parsed", "faims": False}
    frame = pl.concat([pl.DataFrame([old]), pl.DataFrame([new])], how="diagonal_relaxed")
    buf = io.BytesIO()
    frame.write_parquet(buf)
    hub.files["benchmark_latest.parquet"] = buf.getvalue()
    rows = {r["submission_id"]: r for r in client.get("/api/leaderboard").json()["submissions"]}
    assert rows["a"]["lc_model"] is None and rows["a"]["faims"] is None
    assert rows["b"]["lc_model"] == "Evosep One" and rows["b"]["faims"] is False
    assert "run_name" not in rows["a"]
