"""Community redesign P3a, relay side (Space 1.7.0).

/api/submit accepts four optional cohort attributes (spec §A.5 B4 and
decision 12) and stores them: lc_model (mapped onto the canonical LC
vocabulary, a copy of stan.metrics.scoring._LC_MODEL_VOCAB kept identical
here; anything else is ''), lc_flow (nano | capillary | micro),
amount_source (declared | parsed | assumed) and faims (bool, null when
unknown). An unknown value is stored as '' / null; it never rejects a
submission. A client that does not send them is accepted exactly as before,
the v1 completeness gate does not ask for them, and /api/update may patch
them but refuses an unusable value with 422, leaving the stored one alone. The page does not
read them yet (P3b/P3d), so nothing on it changes: the TIC, lookup and P2
hash pins in the other relay tests stay as they were.

Fakes come from tests/test_relay_peg.py: nothing here reaches the network.
"""

from __future__ import annotations

import io

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.test_p3a_capture import NEW_LC_NAMES
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


def test_relay_vocabulary_is_the_clients(relay):
    """lc_model becomes a cohort key (decision 12): the relay and the client
    must name every LC the same way. The relay cannot import stan, so it
    keeps a copy; this is what keeps the copy honest."""
    from stan.metrics.scoring import _LC_MODEL_VOCAB, normalize_lc_model

    assert relay._LC_MODEL_VOCAB == _LC_MODEL_VOCAB
    for name in ("Dionex UltiMate 3000", "Thermo.Vanquish.Neo", "Thermo.EasyNLC1200",
                 "EVOSEP_ONE", "Evosep One (Sampler0)", "nanoElute 2", "Agilent ICF System",
                 "WPS-3000", "", "ACQUITY UPLC M-Class", *HOSTILE_STRINGS):
        assert relay._clean_lc_model(name) == (normalize_lc_model(name) or ""), name[:40]


HOSTILE_STRINGS = (
    "  Vanquish\u200b  Neo\u202e ", "\uff36\uff41\uff4e\uff51\uff55\uff49\uff53\uff48 Neo",
    "my homebuilt pump", "<script>alert(1)</script>", "Robert'); DROP TABLE runs;--",
    "x" * 100000, "x" * 300 + "evosep one",
)


@pytest.mark.parametrize("sent,stored", [
    ("UltiMate 3000", "UltiMate 3000"),
    ("Dionex UltiMate 3000 RSLCnano", "UltiMate 3000"),
    ("  Vanquish\u200b  Neo\u202e ", "Vanquish Neo"),            # zero-width / bidi spliced
    ("\uff36\uff41\uff4e\uff51\uff55\uff49\uff53\uff48 Neo", "Vanquish Neo"),  # fullwidth
    ("my homebuilt pump", ""),                                     # free text: not a cohort
    ("<script>alert(1)</script>", ""),
    ("Robert'); DROP TABLE runs;--", ""),
    ("x" * 100000, ""),
    ("x" * 300 + "evosep one", ""),                                # past the 200-char window
    (42, ""),
    (["Evosep One"], ""),
    ({"name": "Evosep One"}, ""),
])
def test_lc_model_stores_only_vocabulary_names(client, relay, sent, stored):
    row = _p3a(_submit(client, relay, lc_model=sent))
    assert row["lc_model"] == stored
    assert row["lc_model"] in {c for _, c in relay._LC_MODEL_VOCAB} | {""}


@pytest.mark.parametrize("sent,stored", [
    *NEW_LC_NAMES,
    ("Agilent ICF System", ""),          # HyStar's control framework, not an LC
])
def test_relay_stores_the_review_additions(client, relay, sent, stored):
    from stan.metrics.scoring import normalize_lc_model

    assert relay._clean_lc_model(sent) == (normalize_lc_model(sent) or "")
    row = _p3a(_submit(client, relay, lc_model=sent))
    assert row["lc_model"] == stored
    assert row["lc_model"] in {c for _, c in relay._LC_MODEL_VOCAB} | {""}


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


def _stored(client, relay, hub, monkeypatch, **fields) -> str:
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    table = _submit(client, relay, **fields)
    sid = table.column("submission_id")[0].as_py()
    buf = io.BytesIO()
    pq.write_table(table, buf)
    hub.files[f"submissions/{sid}.parquet"] = buf.getvalue()
    return sid


def _stored_p3a(hub, sid) -> dict:
    return _p3a(pq.read_table(io.BytesIO(hub.files[f"submissions/{sid}.parquet"])))


def test_update_patches_valid_values(client, relay, hub, monkeypatch):
    sid = _stored(client, relay, hub, monkeypatch)
    r = client.post(f"/api/update/{sid}", json={
        "lc_model": "Thermo.Vanquish.Neo", "lc_flow": " Nano ", "amount_source": "parsed", "faims": 1})
    assert r.status_code == 200, r.text
    assert _stored_p3a(hub, sid) == {"lc_model": "Vanquish Neo", "lc_flow": "nano",
                                     "amount_source": "parsed", "faims": True}


@pytest.mark.parametrize("patch", [
    {"faims": "maybe"}, {"faims": 2}, {"lc_flow": "turbo"}, {"amount_source": "bogus"},
    {"lc_model": "my homebuilt pump"}, {"lc_model": 42},
    {"lc_flow": "nano", "faims": "maybe"},          # one bad value refuses the whole patch
])
def test_update_refuses_an_invalid_value_and_changes_nothing(client, relay, hub, monkeypatch, patch):
    sid = _stored(client, relay, hub, monkeypatch, lc_model="UltiMate 3000",
                  lc_flow="capillary", amount_source="declared", faims=True)
    before = hub.files[f"submissions/{sid}.parquet"]
    r = client.post(f"/api/update/{sid}", json=patch)
    assert r.status_code == 422, r.text
    assert "nothing was changed" in r.json()["detail"]
    assert hub.files[f"submissions/{sid}.parquet"] == before
    assert hub.uploads == []


def test_update_clears_with_empty_or_null(client, relay, hub, monkeypatch):
    sid = _stored(client, relay, hub, monkeypatch, lc_model="UltiMate 3000",
                  lc_flow="nano", amount_source="parsed", faims=True)
    r = client.post(f"/api/update/{sid}", json={
        "lc_model": "", "lc_flow": None, "amount_source": "", "faims": None})
    assert r.status_code == 200, r.text
    patched = pq.read_table(io.BytesIO(hub.files[f"submissions/{sid}.parquet"]))
    assert patched.schema.field("faims").type == pa.bool_()
    assert _p3a(patched) == {"lc_model": "", "lc_flow": "", "amount_source": "", "faims": None}


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
