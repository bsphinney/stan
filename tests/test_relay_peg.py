"""The HF Space relay's PEG channel (hf_space/app.py) and its deploy script.

The relay is its own deployable (HF Space brettsp/stan), vendored into this
repo in v1.2.0 so changes to it are reviewed and tested here. It is imported
by path, and every Hugging Face call is replaced by an in-memory fake:
nothing in this file reaches the network, the dataset, or the email sender.
Each test gets a freshly imported module, so the commit queue, the PEG store,
the caches and the rate limiter never leak between tests.

Spec: docs/superpowers/specs/2026-09-28-peg-watch-design.md (§4.4, §4.5).
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import logging
import re
import shutil
import statistics
import subprocess
import sys
import time
import types
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
APP_PATH = REPO / "hf_space" / "app.py"
DEPLOY_PATH = REPO / "scripts" / "deploy_hf_space.py"
# hf_space/app.py exactly as vendored from Space commit d041ef68 (SPACE_VERSION 1.1.0).
BASE_RELAY_SHA256 = "d89ea3fd5bcb63b393c702e932af32f15643a45298ee7152d20c6b9def83b4c3"
NOW = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
LATEST = "peg/peg_latest.parquet"
# Optional cohort columns Space 1.7.0 (P3a) appends to every stored submission.
P3A_COLUMNS = ("lc_model", "lc_flow", "amount_source", "faims")
CLAIMS = "identity/claims.json"


# ── fakes ────────────────────────────────────────────────────────────

class FakeHub:
    """In-memory stand-in for the dataset repo, hf_hub_download and HfApi."""

    def __init__(self, cache_dir: Path) -> None:
        self.files: dict[str, bytes] = {}
        self.commits: list[dict] = []
        self.uploads: list[dict] = []
        self.unreachable: set[str] = set()   # downloads that fail like a Hub outage
        self.fail_commits: list[Exception] = []
        self.cache_dir = cache_dir

    def hf_hub_download(self, repo_id, filename, *args, repo_type=None, token=None, **kw):
        from huggingface_hub.errors import LocalEntryNotFoundError, RemoteEntryNotFoundError

        if filename in self.unreachable:
            raise LocalEntryNotFoundError("hub unreachable (test)")
        if filename not in self.files:
            raise RemoteEntryNotFoundError(
                f"{filename} not in repo (test)",
                response=httpx.Response(404, request=httpx.Request("GET", "https://huggingface.co/x")),
            )
        path = self.cache_dir / uuid.uuid4().hex / Path(filename).name
        path.parent.mkdir(parents=True)
        path.write_bytes(self.files[filename])
        return str(path)

    def snapshot_download(self, *args, **kw):
        empty = self.cache_dir / ("snap_" + uuid.uuid4().hex)
        empty.mkdir()
        return str(empty)

    def api(self, *args, **kw) -> "FakeApi":
        return FakeApi(self)


class FakeApi:
    def __init__(self, hub: FakeHub) -> None:
        self.hub = hub

    def create_commit(self, repo_id=None, operations=(), *, commit_message="", repo_type=None, **kw):
        if self.hub.fail_commits:
            raise self.hub.fail_commits.pop(0)
        ops = list(operations)
        files = {op.path_in_repo: op.path_or_fileobj.getvalue() for op in ops}
        self.hub.commits.append({
            "message": commit_message, "paths": [op.path_in_repo for op in ops], "files": files,
        })
        self.hub.files.update(files)

    def upload_file(self, *, path_or_fileobj, path_in_repo, repo_id=None, repo_type=None,
                    commit_message=None, **kw):
        if hasattr(path_or_fileobj, "getvalue"):
            data = path_or_fileobj.getvalue()
        else:
            data = Path(path_or_fileobj).read_bytes()
        self.hub.uploads.append({"path": path_in_repo, "data": data, "message": commit_message})
        self.hub.files[path_in_repo] = data

    def list_repo_files(self, *args, **kw):
        return list(self.hub.files)


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod   # pydantic resolves string annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hub(tmp_path, monkeypatch) -> FakeHub:
    import huggingface_hub

    fake = FakeHub(tmp_path)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake.hf_hub_download)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake.snapshot_download)
    monkeypatch.setattr(huggingface_hub, "HfApi", fake.api)
    return fake


def _prepare_relay(mod: types.ModuleType, monkeypatch) -> None:
    monkeypatch.setattr(mod, "HF_TOKEN", "hf_test_token")
    # The worker thread would sleep 60 s and then drain the queue under the
    # test's feet; tests drain it themselves via _flush_once.
    monkeypatch.setattr(mod, "_ensure_flush_worker_started", lambda: None)
    monkeypatch.setattr(mod, "_send_verification_email", lambda *a, **k: True)


@pytest.fixture
def relay(hub, monkeypatch):
    monkeypatch.delenv("CLAIMS_PEPPER", raising=False)
    name = f"relay_under_test_{uuid.uuid4().hex[:8]}"
    mod = _load_module(APP_PATH, name)
    _prepare_relay(mod, monkeypatch)
    clock = {"now": NOW, "mono": 1000.0}
    monkeypatch.setattr(mod, "_peg_now", lambda: clock["now"])
    monkeypatch.setattr(mod, "_peg_clock", lambda: clock["mono"])
    mod.test_clock = clock
    yield mod
    sys.modules.pop(name, None)


@pytest.fixture
def client(relay) -> TestClient:
    return TestClient(relay.app)


# ── helpers ──────────────────────────────────────────────────────────

def rk(lab: str, i: int) -> str:
    return hashlib.sha256(f"{lab}|{i}".encode()).hexdigest()[:24]


def at(days_before: float) -> str:
    """Noon UTC ``days_before`` days before the pinned as-of date (2026-09-28)."""
    t = datetime(2026, 9, 28, 12, tzinfo=timezone.utc) - timedelta(days=days_before)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(run_key: str | None = None, **over) -> dict:
    rec = {
        "run_key": run_key or rk("default", 0),
        "run_date": "2026-09-25T18:27:00Z",
        "instrument_family": "timsTOF",
        "instrument_model": "timsTOF HT",
        "lc_system": "evosep",
        "lc_model": None,
        "spd": 100,
        "acquisition_mode": "diapasef",
        "sample_type": "hela",
        "amount_ng": 50.0,
        "peg_intensity_pct": 0.353,
        "peg_score": 7.5,
        "peg_n_ions_detected": 2,
        "peg_class": "clean",
        "peg_method": "stan-peg-1",
    }
    rec.update(over)
    return rec


def submit(client: TestClient, name: str, records: list, token: str | None = None,
           xff: str | None = None) -> httpx.Response:
    headers = {}
    if token is not None:
        headers["X-STAN-Auth"] = token
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    return client.post(
        "/api/peg/submit",
        json={"display_name": name, "stan_version": "1.2.0", "records": records},
        headers=headers,
    )


def queued(relay) -> list:
    return list(relay._SUBMIT_QUEUE.queue)


def drain(relay) -> None:
    while not relay._SUBMIT_QUEUE.empty():
        relay._SUBMIT_QUEUE.get_nowait()


def claim_entry(relay, token: str, email: str = "owner@lab.org") -> dict:
    return {"email_hash": relay._hash(email), "token_hash": relay._hash(token),
            "claimed_at": "2026-05-01T00:00:00+00:00"}


def set_claims(hub: FakeHub, entries: dict) -> None:
    hub.files[CLAIMS] = json.dumps(entries).encode()


def rows_of(data: bytes) -> list[dict]:
    return pq.read_table(io.BytesIO(data)).to_pylist()


def item(relay, path_prefix: str):
    matches = [it for it in queued(relay) if it.path_in_repo.startswith(path_prefix)]
    assert len(matches) == 1, [it.path_in_repo for it in queued(relay)]
    return matches[0]


def quartiles(values: list[float]) -> list[float]:
    """Independent reference: linear interpolation, same as PG percentile_cont."""
    return [round(q, 3) for q in statistics.quantiles(values, n=4, method="inclusive")]


# ── identity (spec §4.5, D3) ─────────────────────────────────────────

def test_space_version(client):
    assert client.get("/api/version").json()["version"] == "1.9.0"


def test_unclaimed_name_is_accepted_but_unverified(client, relay):
    r = submit(client, "Tiny Lab", [record()])
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "display_name": "Tiny Lab", "verified": False,
                        "accepted": 1, "unchanged": 0, "rejected": []}
    assert rows_of(item(relay, LATEST).data)[0]["verified"] is False


def test_claimed_name_with_its_token_is_verified(client, relay, hub):
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, "s3cret-token")})
    r = submit(client, "Clogged PeakTail", [record()], token="s3cret-token")
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True
    assert rows_of(item(relay, LATEST).data)[0]["verified"] is True


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_claimed_name_without_its_token_is_refused(client, relay, hub, token):
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, "s3cret-token")})
    r = submit(client, "Clogged PeakTail", [record()], token=token)
    assert r.status_code == 403
    assert "stan community-claim" in r.json()["detail"]
    assert queued(relay) == []


@pytest.mark.parametrize("spoof", ["Clogged​PeakTail ", "Clogged  PeakTail", " Clogged\tPeakTail"])
def test_invisible_characters_do_not_dodge_a_claim(client, relay, hub, spoof):
    """Zero-width and doubled whitespace must not make a claimed name look unclaimed."""
    set_claims(hub, {"CloggedPeakTail": claim_entry(relay, "a"),
                     "Clogged PeakTail": claim_entry(relay, "b")})
    assert submit(client, spoof, [record()]).status_code == 403


# Each renders exactly like "Clogged PeakTail". Only Cf characters were
# dropped before, so every one of these was accepted as a separate,
# unclaimed lab: Hangul fillers (Lo), the combining grapheme joiner and
# variation selectors (Mn), a braille blank (So), a fullwidth C (Lu).
LOOKALIKES = [
    "Clogged PeakTailㅤ", "Clogged PeakTailᅟ", "Clogged PeakTailﾠ",
    "Clogged Peak͏Tail", "Clogged PeakTail️", "Clogged PeakTail\U000e0100",
    "Ｃlogged PeakTail", "Clogged⠀PeakTail", "Clogged Peak­Tail",
    "Clogged PeakTail\U000e0041", "Clogged PeakTail",
]


@pytest.mark.parametrize("spoof", LOOKALIKES)
def test_lookalike_names_do_not_dodge_a_claim(client, relay, hub, spoof):
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, "b")})
    r = submit(client, spoof, [record()])
    assert r.status_code == 403, r.text
    assert queued(relay) == []
    assert submit(client, spoof, [record()], token="b").json()["verified"] is True


def test_clean_text_is_idempotent_keeps_case_and_recomposes(relay):
    for s in LOOKALIKES + ["Café Lab", unicodedata.normalize("NFD", "Café Lab")]:
        once = relay._clean_text(s)
        assert relay._clean_text(once) == once
    assert all(relay._clean_text(s) == "Clogged PeakTail" for s in LOOKALIKES)
    # Live claims hold both spellings; folding case would merge two labs.
    assert relay._clean_text("Clogged Peaktail") != relay._clean_text("Clogged PeakTail")
    # Dropping a joiner between a letter and its accent must not leave NFD behind.
    assert relay._clean_text("Cafe͏́ Lab") == "Café Lab"


@pytest.mark.parametrize("stored", [
    unicodedata.normalize("NFD", "Café Lab"), "Café  Lab", " Café Lab​",
])
def test_a_claim_stored_in_non_canonical_form_still_binds_its_name(client, relay, hub, stored):
    """claims.json keys written by claim-name before canonicalisation were only strip()ped."""
    set_claims(hub, {stored: claim_entry(relay, "owner-token")})
    assert submit(client, "Café Lab", [record()]).status_code == 403
    assert submit(client, "Café Lab", [record()], token="intruder").status_code == 403
    body = submit(client, "Café Lab", [record()], token="owner-token").json()
    assert (body["display_name"], body["verified"]) == ("Café Lab", True)


def test_two_claims_for_one_canonical_name_both_bind_it(client, relay, hub):
    set_claims(hub, {"Double  Lab": claim_entry(relay, "first"), "Double Lab": claim_entry(relay, "second")})
    assert submit(client, "Double Lab", [record()]).status_code == 403
    assert submit(client, "Double Lab", [record()], token="first").json()["verified"] is True
    assert submit(client, "Double Lab", [record()], token="second").json()["verified"] is True


def _claim(client, relay, name: str, email: str = "owner@lab.org") -> str:
    """claim-name + verify-claim, echoing the claim_id as stan setup does; returns the token."""
    r = client.post("/api/claim-name", json={"pseudonym": name, "email": email})
    assert r.status_code == 200, r.text
    claim_id = r.json()["claim_id"]
    pending = relay._pending_codes[relay._clean_text(name)]
    r = client.post("/api/verify-claim",
                    json={"pseudonym": name, "code": pending["code"], "claim_id": claim_id})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.mark.parametrize("typed", ["Double  Space Lab", unicodedata.normalize("NFD", "Proteômica Lab")])
def test_claim_endpoints_store_the_canonical_name(client, relay, hub, typed):
    """A name claimed with a doubled space or pasted in NFD must be the name the owner shares under."""
    token = _claim(client, relay, typed)
    canonical = relay._clean_text(typed)
    assert canonical != typed
    assert list(json.loads(hub.files[CLAIMS])) == [canonical]
    assert submit(client, canonical, [record()]).status_code == 403
    assert submit(client, typed, [record()]).status_code == 403
    body = submit(client, typed, [record()], token=token).json()
    assert (body["display_name"], body["verified"]) == (canonical, True)


def test_reclaiming_replaces_a_non_canonical_key(client, relay, hub):
    set_claims(hub, {"Double  Space Lab": claim_entry(relay, "old-token", email="owner@lab.org")})
    assert client.post("/api/claim-name", json={"pseudonym": "Double Space Lab",
                                                "email": "intruder@else.org"}).status_code == 409
    token = _claim(client, relay, "Double Space Lab", email="owner@lab.org")
    assert list(json.loads(hub.files[CLAIMS])) == ["Double Space Lab"]
    assert submit(client, "Double Space Lab", [record()], token="old-token").status_code == 403
    assert submit(client, "Double Space Lab", [record()], token=token).json()["verified"] is True


@pytest.mark.parametrize("name", ["Anonymous Lab", " anonymous  lab", "x" * 61, "​", "x" * 1000])
def test_claim_name_refuses_names_the_peg_channel_would_refuse(client, relay, name):
    r = client.post("/api/claim-name", json={"pseudonym": name, "email": "a@b.org"})
    assert r.status_code == 400, r.text
    assert relay._pending_codes == {}


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def _verify(client, name: str, code: str, claim_id: str | None = None, xff: str | None = None):
    body = {"pseudonym": name, "code": code}
    if claim_id is not None:
        body["claim_id"] = claim_id
    headers = {"X-Forwarded-For": xff} if xff else {}
    return client.post("/api/verify-claim", json=body, headers=headers)


def test_verify_claim_gives_up_after_five_wrong_codes(client, relay, hub):
    set_claims(hub, {"Lab A": claim_entry(relay, "t")})
    r = client.post("/api/claim-name", json={"pseudonym": "New Lab", "email": "new@lab.org"})
    assert r.status_code == 200
    claim_id = r.json()["claim_id"]
    code = relay._pending_codes["New Lab"]["code"]
    wrong = [_verify(client, "New Lab", _wrong(code), claim_id).status_code for _ in range(5)]
    assert wrong == [403, 403, 403, 403, 429]
    r = _verify(client, "New Lab", code, claim_id)
    assert r.status_code == 400 and "No pending verification" in r.json()["detail"]
    assert list(json.loads(hub.files[CLAIMS])) == ["Lab A"] and hub.uploads == []


def test_strangers_wrong_codes_cannot_spend_the_owners_pending_code(client, relay, hub):
    """Only the caller holding the claim_id can use up a code's five attempts.

    Keyed by name alone, five wrong guesses from anyone threw away the code
    the owner had just been emailed.
    """
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, "old-token", email="owner@lab.org")})
    r = client.post("/api/claim-name", json={"pseudonym": "Clogged PeakTail", "email": "owner@lab.org"},
                    headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200
    claim_id = r.json()["claim_id"]
    code = relay._pending_codes["Clogged PeakTail"]["code"]
    # Without a claim_id (as a STAN that predates it would send), from many addresses ...
    for i in range(3 * relay.CLAIM_MAX_ATTEMPTS):
        assert _verify(client, "Clogged PeakTail", _wrong(code), xff=f"198.51.100.{i}").status_code == 403
    # ... and with a claim_id that is not the one issued.
    for _ in range(3 * relay.CLAIM_MAX_ATTEMPTS):
        assert _verify(client, "Clogged PeakTail", _wrong(code), "forged-claim-id").status_code == 400
    assert _verify(client, "Clogged PeakTail", code, "forged-claim-id").status_code == 400
    assert relay._pending_codes["Clogged PeakTail"].get("attempts", 0) == 0

    r = _verify(client, "Clogged PeakTail", code, claim_id, xff="203.0.113.99")
    assert r.status_code == 200, r.text
    assert json.loads(hub.files[CLAIMS])["Clogged PeakTail"]["token_hash"] == relay._hash(r.json()["token"])


def test_verify_without_a_claim_id_still_works_and_is_capped_per_caller_and_per_name(client, relay, hub):
    """STAN versions before claim_id still verify; their guesses are capped but never spend the code."""
    assert client.post("/api/claim-name", json={"pseudonym": "New Lab", "email": "new@lab.org"}).status_code == 200
    code = relay._pending_codes["New Lab"]["code"]
    one = "198.51.100.1"
    assert [_verify(client, "New Lab", _wrong(code), xff=one).status_code
            for _ in range(relay.CLAIM_MAX_ATTEMPTS)] == [403] * relay.CLAIM_MAX_ATTEMPTS
    assert _verify(client, "New Lab", code, xff=one).status_code == 429, "that caller is out of guesses"
    r = _verify(client, "New Lab", code, xff="198.51.100.2")
    assert r.status_code == 200, "another caller with the emailed code still verifies"

    # Callers rotating addresses share one per-name ceiling on claim_id-less guesses ...
    r = client.post("/api/claim-name", json={"pseudonym": "Newer Lab", "email": "new@lab.org"})
    claim_id = r.json()["claim_id"]
    code = relay._pending_codes["Newer Lab"]["code"]
    for i in range(relay.CLAIM_LEGACY_ATTEMPTS_PER_NAME):
        assert _verify(client, "Newer Lab", _wrong(code), xff=f"198.51.101.{i}").status_code == 403
    assert _verify(client, "Newer Lab", code, xff="198.51.102.1").status_code == 429
    # ... which never locks out the caller holding the claim_id.
    assert _verify(client, "Newer Lab", code, claim_id).status_code == 200


def test_strangers_refused_claims_do_not_use_up_the_owners_claim_budget(client, relay, hub, monkeypatch):
    """Re-claiming is how a lab rotates its token; a stranger must not be able to stop it.

    Keyed by name, three "different email" answers an hour to anyone kept
    the owner from ever being sent a code.
    """
    clock = {"t": 5000.0}
    monkeypatch.setattr(relay, "_claim_clock", lambda: clock["t"])
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, "old-token", email="owner@lab.org")})
    for i in range(20):
        r = client.post("/api/claim-name", json={"pseudonym": "Clogged PeakTail", "email": f"x{i}@evil.org"},
                        headers={"X-Forwarded-For": f"198.51.100.{i}"})
        assert r.status_code == 409
    r = client.post("/api/claim-name", json={"pseudonym": "Clogged PeakTail", "email": "owner@lab.org"},
                    headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200, r.text


def test_claim_name_calls_are_capped_per_caller(client, relay, hub, monkeypatch):
    """Every call counts against its caller, so "different email" answers cannot test emails freely."""
    clock = {"t": 5000.0}
    monkeypatch.setattr(relay, "_claim_clock", lambda: clock["t"])
    set_claims(hub, {"Lab A": claim_entry(relay, "t", email="owner@lab.org")})

    def claim(email, xff="198.51.100.1", name="Lab A"):
        return client.post("/api/claim-name", json={"pseudonym": name, "email": email},
                           headers={"X-Forwarded-For": xff}).status_code

    n = relay.CLAIM_CALLS_PER_CLIENT_HOUR
    assert [claim(f"guess{i}@x.org") for i in range(n)] == [409] * n
    assert claim("guess-next@x.org") == 429
    assert claim("new@lab.org", name="Lab B") == 429, "the cap is per caller, whatever the name"
    assert claim("guess-next@x.org", xff="198.51.100.2") == 409, "another caller has its own"
    clock["t"] += relay.CLAIM_RATE_WINDOW_SEC + 1
    assert claim("guess-next@x.org") == 409


def test_claim_name_issues_at_most_three_codes_per_name_and_email_per_hour(client, relay, hub, monkeypatch):
    clock = {"t": 5000.0}
    monkeypatch.setattr(relay, "_claim_clock", lambda: clock["t"])
    set_claims(hub, {"Lab A": claim_entry(relay, "t", email="owner@lab.org")})

    def claim(name, email="owner@lab.org", xff=None):
        headers = {"X-Forwarded-For": xff} if xff else {}
        return client.post("/api/claim-name", json={"pseudonym": name, "email": email},
                           headers=headers).status_code

    assert [claim("Lab A") for _ in range(3)] == [200, 200, 200]
    assert claim("Lab A", xff="203.0.113.50") == 429, "codes issued are capped whichever address asks"
    assert claim("Lab  A") == 429, "a respelling of the same name shares its budget"
    assert claim("Lab B") == 200, "another name has its own"
    clock["t"] += relay.CLAIM_RATE_WINDOW_SEC + 1
    assert claim("Lab A") == 200


@pytest.mark.parametrize("name", ["Anonymous Lab", "  anonymous   LAB ", "", "​​", "x" * 61])
def test_anonymous_or_empty_name_is_refused(client, relay, name):
    r = submit(client, name, [record()])
    assert r.status_code == 400, r.text
    assert queued(relay) == []


def test_unreadable_claims_fail_closed(client, relay, hub):
    """An outage must not make a claimed name look free."""
    hub.unreachable.add(CLAIMS)
    r = submit(client, "Clogged PeakTail", [record()])
    assert r.status_code == 503
    assert queued(relay) == []


def test_rate_limit_is_30_per_hour_per_client(client, relay):
    for _ in range(30):
        assert submit(client, "", []).status_code == 400   # rejected requests still count
    assert submit(client, "Tiny Lab", [record()]).status_code == 429
    # Another client (right-most X-Forwarded-For entry) has its own budget.
    assert submit(client, "Tiny Lab", [record()], xff="203.0.113.9").status_code == 200
    relay.test_clock["mono"] += 3601
    assert submit(client, "Tiny Lab", [record()]).status_code == 200


def test_more_than_2000_records_is_refused(client, relay):
    r = submit(client, "Tiny Lab", [{}] * 2001)
    assert r.status_code == 413
    assert queued(relay) == []


# ── size limits: the whole table lives in the Space's memory ─────────

def _rows_for(relay, name: str) -> int:
    return sum(1 for (n, _) in relay._PEG_STORE["rows"] if n == name)


def test_rows_per_name_are_capped_but_updates_still_land(client, relay, monkeypatch):
    monkeypatch.setattr(relay, "PEG_MAX_ROWS_PER_NAME", 3000)
    first = [record(rk("cap", i)) for i in range(2000)]
    second = [record(rk("cap", 2000 + i)) for i in range(2000)]
    assert submit(client, "Big Lab", first).json()["accepted"] == 2000
    drain(relay)
    body = submit(client, "Big Lab", second).json()
    assert body["accepted"] == 1000
    assert [x["index"] for x in body["rejected"]] == list(range(1000, 2000))
    assert all("row limit" in x["reason"] for x in body["rejected"])
    assert _rows_for(relay, "Big Lab") == 3000
    drain(relay)
    assert submit(client, "Big Lab", first).json()["unchanged"] == 2000
    changed = record(rk("cap", 5), peg_intensity_pct=9.0, peg_class="heavy", peg_score=90.0)
    body = submit(client, "Big Lab", [changed]).json()
    assert (body["accepted"], body["rejected"]) == (1, []), "an update to a stored run still lands"
    assert submit(client, "Other Lab", [record(rk("o", 0))]).json()["accepted"] == 1


def test_row_limit_counts_rows_loaded_from_the_dataset(client, relay, hub, monkeypatch):
    rows = []
    for i in range(3):
        row = relay._peg_validate_record(record(rk("s", i)), NOW)[0]
        row.update(display_name="Big Lab", verified=False, submitted_at=NOW, first_seen_at=NOW)
        rows.append(row)
    hub.files[LATEST] = relay._peg_rows_to_parquet(rows)
    monkeypatch.setattr(relay, "PEG_MAX_ROWS_PER_NAME", 3)
    body = submit(client, "Big Lab", [record(rk("s", 3))]).json()
    assert (body["accepted"], len(body["rejected"])) == (0, 1)
    assert queued(relay) == []


def test_new_unclaimed_names_are_capped_and_claimed_names_are_exempt(client, relay, hub, monkeypatch):
    monkeypatch.setattr(relay, "PEG_MAX_UNVERIFIED_NAMES", 2)
    set_claims(hub, {"Claimed Lab": claim_entry(relay, "c-token")})
    assert submit(client, "Lab One", [record(rk("1", 0))]).status_code == 200
    assert submit(client, "Lab Two", [record(rk("2", 0))]).status_code == 200
    drain(relay)
    r = submit(client, "Lab Three", [record(rk("3", 0))])
    assert r.status_code == 429 and "stan community-claim" in r.json()["detail"]
    assert queued(relay) == [] and _rows_for(relay, "Lab Three") == 0
    assert submit(client, "Lab One", [record(rk("1", 1))]).json()["accepted"] == 1, \
        "a name already on the board keeps syncing"
    r = submit(client, "Claimed Lab", [record(rk("c", 0))], token="c-token")
    assert r.status_code == 200 and r.json()["verified"] is True


def test_rows_under_unclaimed_names_are_capped_together(client, relay, hub, monkeypatch):
    monkeypatch.setattr(relay, "PEG_MAX_UNVERIFIED_ROWS", 5)
    set_claims(hub, {"Claimed Lab": claim_entry(relay, "c-token")})
    assert submit(client, "Lab One", [record(rk("1", i)) for i in range(3)]).json()["accepted"] == 3
    body = submit(client, "Lab Two", [record(rk("2", i)) for i in range(3)]).json()
    assert body["accepted"] == 2 and [x["index"] for x in body["rejected"]] == [2]
    body = submit(client, "Claimed Lab", [record(rk("c", i)) for i in range(4)], token="c-token").json()
    assert (body["accepted"], body["rejected"]) == (4, [])


def test_stan_version_cannot_forge_a_log_line(client, relay, caplog):
    caplog.set_level(logging.INFO, logger=relay.logger.name)
    forged = "1.2.0\nPEG submit 'Clogged PeakTail' (verified=True, stan 1.2.0): forged" + "x" * 100_000
    r = client.post("/api/peg/submit", json={"display_name": "Tiny Lab", "stan_version": forged,
                                             "records": [record()]})
    assert r.status_code == 200, r.text
    submit(client, "Tiny Lab", [record(rk("t", 1))])
    lines = [rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("PEG submit")]
    assert len(lines) == 2
    assert "\n" not in lines[0] and len(lines[0]) < 500 and "stan ?)" in lines[0]
    assert "stan 1.2.0)" in lines[1], "a real version still reads as itself"


# ── validation ───────────────────────────────────────────────────────

def test_bad_records_are_rejected_and_good_ones_still_accepted(client, relay):
    future = (NOW + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    batch = [
        record(rk("v", 0)),                                         # 0 good
        record(rk("v", 1), lc_system="custom"),                     # 1 not evosep/other
        record(rk("v", 2), peg_class="unknown"),                    # 2 failure sentinel
        record(rk("v", 3), peg_intensity_pct=100.5),                # 3 pct > 100
        record(rk("v", 4), run_date=future),                        # 4 future
        record("not-a-run-key"),                                    # 5 bad run_key
        record(rk("v", 6), lc_system="other", lc_model="Vanquish Neo"),  # 6 good
        record(rk("v", 7), peg_intensity_pct=None),                 # 7 unmeasured, never 0
        record(rk("v", 8), spd=0),                                  # 8 spd out of range
        record(rk("v", 9), peg_score=True),                         # 9 bool is not a number
        "not an object",                                            # 10
        record(rk("v", 11), run_date="yesterday"),                  # 11 unparseable
        record(rk("v", 12), peg_n_ions_detected=501),               # 12 ions out of range
        record(rk("v", 13), run_date=(NOW + timedelta(hours=20)).isoformat()),  # 13 good: +1 day slack
    ]
    r = submit(client, "Tiny Lab", batch)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 3
    assert [x["index"] for x in body["rejected"]] == [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
    assert all(x["reason"] for x in body["rejected"])
    stored = rows_of(item(relay, LATEST).data)
    assert sorted(r["run_key"] for r in stored) == sorted([rk("v", 0), rk("v", 6), rk("v", 13)])
    other = next(r for r in stored if r["run_key"] == rk("v", 6))
    assert (other["lc_system"], other["lc_model"]) == ("other", "Vanquish Neo")


def test_unknown_fields_such_as_run_name_are_never_stored(client, relay):
    r = submit(client, "Tiny Lab", [record(run_name="secret_patient_042.d")])
    assert r.json()["accepted"] == 1
    for it in queued(relay):
        assert b"secret_patient_042" not in it.data
        assert "run_name" not in pq.read_table(io.BytesIO(it.data)).column_names


def test_duplicate_run_key_in_one_batch_keeps_the_later_record(client, relay):
    r = submit(client, "Tiny Lab", [record(peg_intensity_pct=1.0), record(peg_intensity_pct=2.0)])
    body = r.json()
    assert body["accepted"] == 1
    assert [x["index"] for x in body["rejected"]] == [0]
    assert rows_of(item(relay, LATEST).data)[0]["peg_intensity_pct"] == 2.0


# ── storage: merge, unchanged, commits ───────────────────────────────

def test_identical_resubmission_is_unchanged_and_enqueues_nothing(client, relay):
    batch = [record(rk("u", i), peg_intensity_pct=0.1 * i) for i in range(5)]
    assert submit(client, "Tiny Lab", batch).json()["accepted"] == 5
    drain(relay)
    body = submit(client, "Tiny Lab", batch).json()
    assert (body["accepted"], body["unchanged"], body["rejected"]) == (0, 5, [])
    assert queued(relay) == []


def test_changed_record_enqueues_full_table_and_audit_file(client, relay):
    batch = [record(rk("c", 0)), record(rk("c", 1))]
    submit(client, "Tiny Lab", batch)
    submit(client, "Other Lab", [record(rk("c", 9))])
    drain(relay)

    relay.test_clock["now"] = NOW + timedelta(hours=1)
    batch[1] = record(rk("c", 1), peg_intensity_pct=4.2, peg_class="moderate", peg_score=55.0)
    body = submit(client, "Tiny Lab", batch).json()
    assert (body["accepted"], body["unchanged"]) == (1, 1)

    paths = sorted(it.path_in_repo for it in queued(relay))
    assert len(paths) == 2
    assert paths[0] == LATEST
    assert paths[1].startswith("peg/submissions/20260928T160000Z_") and paths[1].endswith(".parquet")
    assert len(Path(paths[1]).stem.split("_")[1]) == 8

    latest = {(r["display_name"], r["run_key"]): r for r in rows_of(item(relay, LATEST).data)}
    assert len(latest) == 3, "the full table, every lab"
    changed = latest[("Tiny Lab", rk("c", 1))]
    assert changed["peg_intensity_pct"] == 4.2
    assert changed["submitted_at"] == NOW + timedelta(hours=1)
    assert changed["first_seen_at"] == NOW, "first_seen_at survives an update"
    assert latest[("Tiny Lab", rk("c", 0))]["submitted_at"] == NOW

    audit = rows_of(item(relay, "peg/submissions/").data)
    assert [(r["display_name"], r["run_key"]) for r in audit] == [("Tiny Lab", rk("c", 1))]


def test_parquet_schema_is_explicit(client, relay):
    submit(client, "Tiny Lab", [record()])
    schema = pq.read_table(io.BytesIO(item(relay, LATEST).data)).schema
    assert schema.names == [
        "display_name", "run_key", "run_date", "instrument_family", "instrument_model",
        "lc_system", "lc_model", "spd", "acquisition_mode", "sample_type", "amount_ng",
        "peg_intensity_pct", "peg_score", "peg_n_ions_detected", "peg_class", "peg_method",
        "verified", "submitted_at", "first_seen_at",
    ]
    assert str(schema.field("run_date").type) == "timestamp[us, tz=UTC]"
    assert str(schema.field("peg_intensity_pct").type) == "double"
    assert str(schema.field("spd").type) == "int32"


def test_existing_dataset_table_is_loaded_and_kept(client, relay, hub):
    """A restart reloads peg_latest.parquet; a resync is 'unchanged', other labs survive."""
    seed = relay._peg_validate_record(record(rk("s", 0)), NOW)[0]
    seed.update(display_name="Old Lab", verified=False, submitted_at=NOW - timedelta(days=9),
                first_seen_at=NOW - timedelta(days=30))
    mine = relay._peg_validate_record(record(rk("s", 1)), NOW)[0]
    mine.update(display_name="Tiny Lab", verified=False, submitted_at=NOW - timedelta(days=9),
                first_seen_at=NOW - timedelta(days=9))
    hub.files[LATEST] = relay._peg_rows_to_parquet([seed, mine])

    body = submit(client, "Tiny Lab", [record(rk("s", 1))]).json()
    assert (body["accepted"], body["unchanged"]) == (0, 1)
    assert queued(relay) == []

    body = submit(client, "Tiny Lab", [record(rk("s", 2))]).json()
    assert body["accepted"] == 1
    latest = rows_of(item(relay, LATEST).data)
    assert {(r["display_name"], r["run_key"]) for r in latest} == {
        ("Old Lab", rk("s", 0)), ("Tiny Lab", rk("s", 1)), ("Tiny Lab", rk("s", 2))}


def test_unreadable_store_refuses_writes_instead_of_overwriting(client, relay, hub):
    """If peg_latest.parquet cannot be read, a write would replace every lab's history."""
    hub.files[LATEST] = b"exists but the Hub is down"
    hub.unreachable.add(LATEST)
    assert submit(client, "Tiny Lab", [record()]).status_code == 503
    assert client.get("/api/peg/leaderboard").status_code == 503
    assert queued(relay) == []

    hub.unreachable.clear()
    hub.files.pop(LATEST)
    relay.test_clock["mono"] += relay.PEG_LOAD_RETRY_SEC + 1
    assert submit(client, "Tiny Lab", [record()]).status_code == 200


def test_claiming_a_name_re_marks_its_rows_verified(client, relay, hub):
    batch = [record(rk("k", i)) for i in range(3)]
    assert submit(client, "Kilo Lab", batch).json()["verified"] is False
    drain(relay)
    set_claims(hub, {"Kilo Lab": claim_entry(relay, "kilo-token")})
    body = submit(client, "Kilo Lab", batch, token="kilo-token").json()
    assert (body["verified"], body["accepted"], body["unchanged"]) == (True, 3, 0)
    drain(relay)
    assert submit(client, "Kilo Lab", batch, token="kilo-token").json()["unchanged"] == 3


# ── the batched commit worker ────────────────────────────────────────

def test_peg_files_commit_through_the_batch_worker(client, relay, hub):
    submit(client, "Tiny Lab", [record()])
    assert relay._flush_once(FakeApi(hub), relay.FLUSH_INTERVAL_SEC) == relay.FLUSH_INTERVAL_SEC
    assert len(hub.commits) == 1
    assert hub.commits[0]["message"] == "PEG share update (2 files)"
    assert LATEST in hub.commits[0]["paths"]
    assert queued(relay) == []


def test_stale_copy_of_latest_is_never_committed_over_a_newer_one(relay, hub):
    # A failed commit re-queues its items behind anything queued meanwhile.
    relay._queue_file(LATEST, b"v2", version=2)
    relay._queue_file(LATEST, b"v1", version=1)
    relay._queue_submission("abc", b"row")
    relay._flush_once(FakeApi(hub), 60)
    assert hub.commits[0]["files"] == {LATEST: b"v2", "submissions/abc.parquet": b"row"}
    assert hub.commits[0]["message"] == "Batch submit 1 runs + PEG share update (1 file)"


def test_rate_limited_commit_backs_off_and_retries(client, relay, hub):
    submit(client, "Tiny Lab", [record()])
    hub.fail_commits = [RuntimeError("429 Client Error: Too Many Requests")] * 2
    api = FakeApi(hub)
    assert relay._flush_once(api, 60) == 120
    assert relay._flush_once(api, 600) == 900, "capped at 15 minutes"
    assert len(queued(relay)) == 2, "items re-queued, not lost"
    assert relay._flush_once(api, 900) == relay.FLUSH_INTERVAL_SEC
    assert len(hub.commits) == 1 and queued(relay) == []


def test_other_commit_errors_retry_at_the_normal_interval(client, relay, hub):
    submit(client, "Tiny Lab", [record()])
    hub.fail_commits = [RuntimeError("500 Server Error")]
    assert relay._flush_once(FakeApi(hub), 240) == relay.FLUSH_INTERVAL_SEC
    assert len(queued(relay)) == 2


# ── /api/submit must behave exactly as before ────────────────────────

SUBMIT_PAYLOAD = {
    "stan_version": "1.1.12",
    "display_name": "Clogged PeakTail",
    "instrument_family": "Exploris",
    "instrument_model": "Orbitrap Exploris 480",
    "acquisition_mode": "dda",
    "spd": 38,
    "gradient_length_min": 30,
    "cohort_id": "Exploris_30spd_low",
    "n_psms": 21000,
    "n_peptides": 16000,
    "n_proteins": 3400,
    "lc_system": "custom",
    "fingerprint": "0123456789abcdef",
    "diann_version": "",
    "run_name": "FL20260901_Hela_50ng_DDA_1.raw",
    "run_date": "2026-09-01T10:00:00+00:00",
    "tic_rt_bins": [1.0, 2.0, 3.0],
    "tic_intensity": [10.0, 20.0, 5.0],
    "ms1_signal": 1.5e10,
}


def _run_submit_and_one_flush(mod, hub, monkeypatch) -> tuple[dict, list[dict]]:
    """POST /api/submit with pinned uuid/clock, then run the REAL worker loop once."""
    fixed = uuid.UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(mod, "uuid", types.SimpleNamespace(uuid4=lambda: fixed))

    class PinnedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(mod, "datetime", PinnedDateTime)
    response = TestClient(mod.app).post("/api/submit", json=SUBMIT_PAYLOAD)

    class StopLoop(Exception):
        pass

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise StopLoop

    monkeypatch.setattr(mod, "time", types.SimpleNamespace(
        sleep=fake_sleep, time=time.time, monotonic=time.monotonic))
    hub.commits.clear()
    with pytest.raises(StopLoop):
        mod._flush_worker()
    assert sleeps == [mod.FLUSH_INTERVAL_SEC] * 2
    return response.json(), list(hub.commits)


def _base_relay_source() -> str | None:
    """hf_space/app.py as vendored (1.1.0), from git history; None if unavailable."""
    try:
        commits = subprocess.run(
            ["git", "-C", str(REPO), "log", "--format=%H", "--", "hf_space/app.py"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    for commit in commits:
        try:
            blob = subprocess.run(
                ["git", "-C", str(REPO), "show", f"{commit}:hf_space/app.py"],
                capture_output=True, check=True, timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if hashlib.sha256(blob).hexdigest() == BASE_RELAY_SHA256:
            return blob.decode("utf-8")
    return None


def test_api_submit_commits_byte_for_byte_what_the_base_relay_did(hub, monkeypatch, tmp_path):
    """Same request against the vendored 1.1.0 relay and this one: same bytes, path, message."""
    source = _base_relay_source()
    if source is None:
        pytest.skip("base relay (sha256 d89ea3fd...) not reachable in git history")
    base_path = tmp_path / "relay_base.py"
    base_path.write_text(source)
    # The 1.1.0 relay imports CommitOperationAdd from huggingface_hub.hf_api,
    # a re-export huggingface_hub 2.0 dropped (it cannot even import there,
    # which is what the live Space would hit on its next rebuild). Restore
    # the old path so the comparison still runs; the class is the same.
    import huggingface_hub
    import huggingface_hub.hf_api as hf_api_mod
    monkeypatch.setattr(hf_api_mod, "CommitOperationAdd", huggingface_hub.CommitOperationAdd, raising=False)
    results = []
    for path in (base_path, APP_PATH):
        name = f"relay_cmp_{uuid.uuid4().hex[:8]}"
        mod = _load_module(path, name)
        try:
            _prepare_relay(mod, monkeypatch)
            results.append(_run_submit_and_one_flush(mod, hub, monkeypatch))
        finally:
            sys.modules.pop(name, None)
    (base_resp, base_commits), (new_resp, new_commits) = results
    assert new_resp == base_resp
    assert len(base_commits) == len(new_commits) == 1
    assert new_commits[0]["message"] == base_commits[0]["message"] == "Batch submit 1 runs"
    assert new_commits[0]["paths"] == base_commits[0]["paths"]
    # Space 1.7.0 (P3a) deliberately appends four optional cohort columns to
    # the stored row; byte equality ended there. Everything the 1.1.0 relay
    # wrote is still written, unchanged, and the new columns read "not
    # recorded" for a client that does not send them. Space 1.9.0 (P3c)
    # appends name_verified, false for a submission without a token.
    path = new_commits[0]["paths"][0]
    base_t = pq.read_table(io.BytesIO(base_commits[0]["files"][path]))
    new_t = pq.read_table(io.BytesIO(new_commits[0]["files"][path]))
    assert new_t.column_names == base_t.column_names + list(P3A_COLUMNS) + ["name_verified"]
    assert new_t.select(base_t.column_names).equals(base_t)
    assert new_t.select(list(P3A_COLUMNS) + ["name_verified"]).to_pylist() == [
        {"lc_model": "", "lc_flow": "", "amount_source": "", "faims": None, "name_verified": False}]


def test_api_submit_still_writes_one_submissions_file(relay, hub, monkeypatch):
    """Structural check that does not need git history."""
    resp, commits = _run_submit_and_one_flush(relay, hub, monkeypatch)
    assert resp == {"submission_id": "12345678-1234-5678-1234-567812345678",
                    "cohort_id": "Exploris_30spd_low", "status": "accepted"}
    assert commits[0]["message"] == "Batch submit 1 runs"
    assert commits[0]["paths"] == ["submissions/12345678-1234-5678-1234-567812345678.parquet"]
    table = pq.read_table(io.BytesIO(commits[0]["files"][commits[0]["paths"][0]]))
    assert table.num_columns == 44 + len(P3A_COLUMNS) + 1 and table.num_rows == 1   # + name_verified (1.9.0)
    assert table.column("tic_intensity").to_pylist() == ["[10.0, 20.0, 5.0]"]


# ── aggregates: a synthetic multi-lab community ──────────────────────

def _seed_board(client, relay, hub) -> None:
    """Eight labs; as_of 2026-09-28, 30-day window = Aug 30 .. Sep 28 (UTC).

    Alpha   verified, 9 runs (pct 0.1..0.9, 7 clean), 6 prior-window runs at 2.0
    Bravo   6 runs (pct 3..5, 4 heavy), only 3 prior-window runs
    Charlie 3 runs: unranked
    Delta   5 runs, same median as Alpha but fewer clean; prior median 0.55
    Echo    other LC, cleanest numbers of all: must never be ranked
    Foxtrot Evosep at 36 SPD: not an Evosep method, not a cohort
    Golf    Evosep 60 SPD
    Hotel   Orbitrap on other LC
    """
    set_claims(hub, {"Alpha": claim_entry(relay, "alpha-token")})
    alpha = [record(rk("Alpha", k), run_date=at(k), peg_intensity_pct=round(0.1 * k, 3),
                    peg_class="trace" if k >= 8 else "clean") for k in range(1, 10)]
    alpha += [record(rk("Alpha", 100 + j), run_date="2026-08-15T12:00:00Z",
                     peg_intensity_pct=2.0, peg_class="heavy", peg_score=80.0) for j in range(6)]
    bravo = [record(rk("Bravo", k), run_date=at(k), peg_intensity_pct=p,
                    peg_class="heavy" if p >= 4 else "moderate", peg_score=75.0)
             for k, p in zip(range(2, 8), [3.0, 3.0, 4.0, 4.0, 5.0, 5.0])]
    bravo += [record(rk("Bravo", 100 + j), run_date="2026-08-20T12:00:00Z", peg_intensity_pct=1.0)
              for j in range(3)]
    charlie = [record(rk("Charlie", k), run_date=at(k), peg_intensity_pct=0.05) for k in (3, 4, 5)]
    delta = [record(rk("Delta", k), run_date=at(k), peg_intensity_pct=p,
                    peg_class="clean" if p <= 0.5 else "trace", instrument_model="timsTOF Pro 2")
             for k, p in zip(range(10, 15), [0.3, 0.4, 0.5, 0.6, 0.7])]
    delta += [record(rk("Delta", 100 + j), run_date="2026-08-10T12:00:00Z", peg_intensity_pct=0.55)
              for j in range(5)]
    echo = [record(rk("Echo", k), run_date=at(k), peg_intensity_pct=0.01, lc_system="other")
            for k in range(1, 11)]
    foxtrot = [record(rk("Foxtrot", k), run_date=at(k), spd=36, peg_intensity_pct=0.02)
               for k in range(1, 7)]
    golf = [record(rk("Golf", k), run_date=at(k), spd=60, peg_intensity_pct=1.0) for k in range(1, 6)]
    hotel = [record(rk("Hotel", k), run_date=at(k), spd=38, lc_system="other",
                    instrument_family="Orbitrap", instrument_model="Orbitrap Exploris 480",
                    peg_intensity_pct=0.2) for k in range(1, 8)]
    for name, rows, token in [("Alpha", alpha, "alpha-token"), ("Bravo", bravo, None),
                              ("Charlie", charlie, None), ("Delta", delta, None),
                              ("Echo", echo, None), ("Foxtrot", foxtrot, None),
                              ("Golf", golf, None), ("Hotel", hotel, None)]:
        r = submit(client, name, rows, token=token)
        assert r.status_code == 200 and not r.json()["rejected"], r.text


def test_leaderboard_ranking_min_runs_badges_change_and_weekly(client, relay, hub):
    _seed_board(client, relay, hub)
    r = client.get("/api/peg/leaderboard", params={"family": "timsTOF", "spd": 100, "window": 30})
    assert r.status_code == 200, r.text
    board = r.json()
    assert set(board) == {"generated_at", "as_of", "window_days", "family", "spd",
                          "cohorts", "ranked", "unranked", "community"}
    assert (board["as_of"], board["window_days"], board["family"], board["spd"]) == \
        ("2026-09-28", 30, "timsTOF", 100)
    assert board["generated_at"] == "2026-09-28T15:00:00Z"

    ranked = board["ranked"]
    assert [(x["rank"], x["display_name"]) for x in ranked] == [(1, "Alpha"), (2, "Delta"), (3, "Bravo")]
    alpha, delta, bravo = ranked
    assert set(alpha) == {"rank", "display_name", "verified", "instrument_models", "n_runs",
                          "median_pct", "clean_pct", "heavy_pct", "change_pct", "weekly", "badges"}
    assert (alpha["verified"], delta["verified"]) == (True, False)
    assert (alpha["n_runs"], alpha["median_pct"], alpha["clean_pct"], alpha["heavy_pct"]) == (9, 0.5, 78, 0)
    assert (delta["median_pct"], delta["clean_pct"]) == (0.5, 60), "tie on median: more clean ranks higher"
    assert (bravo["median_pct"], bravo["heavy_pct"]) == (4.0, 67)
    assert alpha["change_pct"] == -75            # 2.0 -> 0.5
    assert delta["change_pct"] == -9             # 0.55 -> 0.5
    assert bravo["change_pct"] is None           # only 3 runs in the previous window
    assert alpha["badges"] == ["cleanest", "most_improved"]
    assert delta["badges"] == [] and bravo["badges"] == []
    assert delta["instrument_models"] == ["timsTOF Pro 2"]

    # 12 trailing weeks ending Sep 28: [.., Aug 11-17 (index 5), .., Sep 15-21, Sep 22-28]
    assert alpha["weekly"] == [None] * 5 + [2.0] + [None] * 4 + [0.8, 0.35]

    assert board["unranked"] == [{"display_name": "Charlie", "verified": False, "n_runs": 3}]
    pooled = [round(0.1 * k, 3) for k in range(1, 10)] + [3.0, 3.0, 4.0, 4.0, 5.0, 5.0] \
        + [0.05] * 3 + [0.3, 0.4, 0.5, 0.6, 0.7]
    p25, p50, p75 = quartiles(pooled)
    assert board["community"] == {"n_labs": 4, "n_runs": 23, "p25_pct": p25,
                                  "median_pct": p50, "p75_pct": p75}

    names = {x["display_name"] for x in ranked} | {x["display_name"] for x in board["unranked"]}
    assert "Echo" not in names, "other-LC labs are never ranked"
    assert board["cohorts"] == [
        {"family": "timsTOF", "spd": 100, "n_labs": 4, "n_runs_365d": 37},
        {"family": "timsTOF", "spd": 60, "n_labs": 1, "n_runs_365d": 5},
    ], "36 SPD is not an Evosep method and never forms a cohort"


def test_leaderboard_longer_window_and_single_lab_gets_no_cleanest_badge(client, relay, hub):
    _seed_board(client, relay, hub)
    board = client.get("/api/peg/leaderboard", params={"window": 90}).json()
    alpha = next(x for x in board["ranked"] if x["display_name"] == "Alpha")
    assert alpha["n_runs"] == 15 and alpha["change_pct"] is None
    golf = client.get("/api/peg/leaderboard", params={"spd": 60}).json()
    assert [x["display_name"] for x in golf["ranked"]] == ["Golf"]
    assert golf["ranked"][0]["badges"] == [], "cleanest needs at least two ranked labs"


def test_leaderboard_off_ladder_spd_unknown_family_and_bad_window(client, relay, hub):
    _seed_board(client, relay, hub)
    for params in ({"spd": 36}, {"family": "Astral"}):
        board = client.get("/api/peg/leaderboard", params=params).json()
        assert board["ranked"] == [] and board["unranked"] == []
        assert board["community"] == {"n_labs": 0, "n_runs": 0, "p25_pct": None,
                                      "median_pct": None, "p75_pct": None}
    assert client.get("/api/peg/leaderboard", params={"family": "TIMSTOF"}).json()["family"] == "timsTOF"
    assert client.get("/api/peg/leaderboard", params={"window": 45}).status_code == 400
    assert client.get("/api/peg/lc-compare", params={"window": 7}).status_code == 400


def test_empty_store_serves_an_empty_board(client):
    board = client.get("/api/peg/leaderboard").json()
    assert (board["ranked"], board["unranked"], board["cohorts"]) == ([], [], [])
    assert (board["family"], board["spd"], board["window_days"]) == ("timsTOF", 100, 30)


def test_change_from_a_near_zero_median_is_blank(relay):
    """UC Davis at window=365 showed "+80150 %": previous-year medians of 0.004-0.009 %."""
    assert relay._peg_change_pct(3.21, 0.004) is None
    assert relay._peg_change_pct(0.01, 0.02) is None
    assert relay._peg_change_pct(0.0, 0.0) is None
    assert relay._peg_change_pct(7.0, 12.0) == -42
    assert relay._peg_change_pct(0.15, 0.3) == -50
    assert relay._peg_change_pct(1.0, None) is None


def test_most_improved_needs_a_real_fall_not_noise_at_the_floor(client, relay):
    def lab(name, prev_pct, cur_pct):
        rows = [record(rk(name, k), run_date=at(k), peg_intensity_pct=cur_pct) for k in range(1, 6)]
        rows += [record(rk(name, 100 + j), run_date="2026-08-15T12:00:00Z", peg_intensity_pct=prev_pct)
                 for j in range(5)]
        assert submit(client, name, rows).json()["accepted"] == 10

    lab("Tiny Noise", 0.02, 0.01)     # -50 %, from below the floor
    lab("Small Drop", 0.3, 0.15)      # -50 %, but only 0.15 percentage points
    lab("Big Fix", 12.0, 7.0)         # -42 %, 5 percentage points
    ranked = {r["display_name"]: r for r in client.get("/api/peg/leaderboard").json()["ranked"]}
    assert ranked["Tiny Noise"]["change_pct"] is None
    assert ranked["Small Drop"]["change_pct"] == -50
    assert ranked["Big Fix"]["change_pct"] == -42
    assert "most_improved" in ranked["Big Fix"]["badges"]
    assert "most_improved" not in ranked["Tiny Noise"]["badges"] + ranked["Small Drop"]["badges"]


def test_most_improved_needs_a_second_lab_like_cleanest(client, relay):
    """A lone lab has no one to out-improve; the E2E run showed it collecting the badge."""
    rows = [record(rk("Solo", k), run_date=at(k), peg_intensity_pct=7.0) for k in range(1, 6)]
    rows += [record(rk("Solo", 100 + j), run_date="2026-08-15T12:00:00Z", peg_intensity_pct=12.0)
             for j in range(5)]
    assert submit(client, "Solo", rows).json()["accepted"] == 10
    (solo,) = client.get("/api/peg/leaderboard").json()["ranked"]
    assert solo["change_pct"] == -42
    assert solo["badges"] == []


def test_family_spelling_cannot_be_changed_by_one_client(client, relay, hub):
    """The dashboard matches cohorts by family with ===, so the echoed spelling must be stable."""
    set_claims(hub, {"Alpha": claim_entry(relay, "alpha-token")})
    alpha = [record(rk("A", k), run_date=at(k)) for k in range(1, 6)]
    alpha += [record(rk("A", 10 + k), run_date=at(k), lc_system="other", instrument_family="Q Exactive",
                     instrument_model="Q Exactive HF") for k in range(1, 6)]
    assert submit(client, "Alpha", alpha, token="alpha-token").json()["accepted"] == 10
    caps = [record(rk("C", k), run_date=at(1 + k % 5), instrument_family="TIMSTOF") for k in range(2000)]
    caps += [record(rk("C", 5000 + k), run_date=at(1), lc_system="other", instrument_family="Q EXACTIVE",
                    instrument_model="Q Exactive HF") for k in range(50)]
    body = submit(client, "Caps Lab", caps[:2000]).json()
    assert body["accepted"] == 2000 and body["verified"] is False
    assert submit(client, "Caps Lab", caps[2000:]).json()["accepted"] == 50

    assert {r["instrument_family"] for r in relay._PEG_STORE["rows"].values()} == {"timsTOF", "Q Exactive",
                                                                                  "Q EXACTIVE"}
    for family in ("timsTOF", "TIMSTOF"):
        board = client.get("/api/peg/leaderboard", params={"family": family}).json()
        assert board["family"] == "timsTOF"
        assert {c["family"] for c in board["cohorts"]} == {"timsTOF"}
        lc = client.get("/api/peg/lc-compare", params={"family": family}).json()
        assert lc["family"] == "timsTOF"
    # A family STAN has no fixed spelling for: the first verified spelling wins, not the majority.
    lc = client.get("/api/peg/lc-compare", params={"family": "q exactive"}).json()
    assert lc["family"] == "Q Exactive"
    assert sorted(f["family"] for f in lc["families"]) == ["Q Exactive", "timsTOF"]


def test_relay_family_spellings_are_the_ones_stan_sends(relay):
    """_PEG_FAMILY_SPELLING mirrors stan.community.submit._instrument_family; keep them together."""
    from stan.community.submit import _instrument_family

    models = ["timsTOF HT", "timsTOF Pro 2", "Orbitrap Astral", "Orbitrap Exploris 480",
              "Orbitrap Fusion Lumos", "Orbitrap Eclipse", "Orbitrap Elite"]
    assert {_instrument_family(m) for m in models} == set(relay._PEG_FAMILY_SPELLING.values())


def test_unverified_rows_under_a_verified_name_do_not_count(client, relay, hub):
    _seed_board(client, relay, hub)
    spoof = relay._peg_validate_record(
        record(rk("Alpha", 999), run_date=at(1), peg_intensity_pct=60.0, peg_class="heavy"), NOW)[0]
    spoof.update(display_name="Alpha", verified=False, submitted_at=NOW, first_seen_at=NOW)
    with relay._PEG_LOCK:
        relay._PEG_STORE["rows"][("Alpha", spoof["run_key"])] = spoof
        relay._PEG_STORE["version"] += 1
    alpha = client.get("/api/peg/leaderboard").json()["ranked"][0]
    assert (alpha["display_name"], alpha["n_runs"], alpha["median_pct"]) == ("Alpha", 9, 0.5)


def test_aggregates_are_cached_for_five_minutes_but_follow_new_data(client, relay, hub):
    _seed_board(client, relay, hub)
    first = client.get("/api/peg/leaderboard").json()
    relay.test_clock["now"] = NOW + timedelta(minutes=1)
    assert client.get("/api/peg/leaderboard").json()["generated_at"] == first["generated_at"]
    relay.test_clock["mono"] += relay.PEG_CACHE_TTL_SEC + 1
    assert client.get("/api/peg/leaderboard").json()["generated_at"] == "2026-09-28T15:01:00Z"

    relay.test_clock["now"] = NOW + timedelta(minutes=2)
    submit(client, "Charlie", [record(rk("Charlie", k), run_date=at(k), peg_intensity_pct=0.05)
                               for k in (6, 7)])
    board = client.get("/api/peg/leaderboard").json()
    assert board["generated_at"] == "2026-09-28T15:02:00Z", "an accepted change is not hidden by the cache"
    assert "Charlie" in [x["display_name"] for x in board["ranked"]]


def test_trend_weekly_bands(client, relay, hub):
    _seed_board(client, relay, hub)
    body = client.get("/api/peg/trend", params={"family": "timsTOF", "spd": 100, "weeks": 4}).json()
    assert set(body) == {"weeks"}
    weeks = body["weeks"]
    assert [w["week_start"] for w in weeks] == ["2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22"]
    assert weeks[0] == {"week_start": "2026-09-01", "n_labs": 0, "n_runs": 0,
                        "p25": None, "p50": None, "p75": None}
    last_week = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6] + [3.0, 3.0, 4.0, 4.0, 5.0] + [0.05] * 3
    p25, p50, p75 = quartiles(last_week)
    assert weeks[-1] == {"week_start": "2026-09-22", "n_labs": 3, "n_runs": 14,
                         "p25": p25, "p50": p50, "p75": p75}
    assert len(client.get("/api/peg/trend").json()["weeks"]) == 52
    assert len(client.get("/api/peg/trend", params={"weeks": 0}).json()["weeks"]) == 1
    assert len(client.get("/api/peg/trend", params={"weeks": 10_000}).json()["weeks"]) == 260
    assert all(w["n_runs"] == 0 for w in client.get(
        "/api/peg/trend", params={"spd": 36}).json()["weeks"])


def test_lc_compare_groups_and_families(client, relay, hub):
    _seed_board(client, relay, hub)
    body = client.get("/api/peg/lc-compare", params={"family": "timsTOF", "window": 90}).json()
    assert set(body) == {"family", "window_days", "as_of", "groups", "families"}
    assert (body["family"], body["window_days"], body["as_of"]) == ("timsTOF", 90, "2026-09-28")
    evosep, other = body["groups"]
    assert (evosep["lc"], other["lc"]) == ("evosep", "other")
    # Every Evosep timsTOF run counts here, whatever its SPD (Foxtrot's 36 included).
    assert (evosep["n_labs"], evosep["n_runs"]) == (6, 15 + 9 + 3 + 10 + 6 + 5)
    assert other == {"lc": "other", "n_labs": 1, "n_runs": 10, "p25_pct": 0.01,
                     "median_pct": 0.01, "p75_pct": 0.01, "clean_pct": 100, "heavy_pct": 0,
                     "weekly": [None] * 24 + [0.01, 0.01]}
    assert len(evosep["weekly"]) == 26
    assert body["families"] == [
        {"family": "timsTOF", "evosep_runs": 48, "other_runs": 10, "evosep_labs": 6, "other_labs": 1},
        {"family": "Orbitrap", "evosep_runs": 0, "other_runs": 7, "evosep_labs": 0, "other_labs": 1},
    ]

    orbi = client.get("/api/peg/lc-compare", params={"family": "Orbitrap"}).json()
    empty, orbi_other = orbi["groups"]
    assert empty == {"lc": "evosep", "n_labs": 0, "n_runs": 0, "p25_pct": None, "median_pct": None,
                     "p75_pct": None, "clean_pct": None, "heavy_pct": None, "weekly": [None] * 26}
    assert (orbi_other["n_runs"], orbi_other["median_pct"]) == (7, 0.2)


# ── claims pepper (spec §4.5 "Claims privacy") ───────────────────────

def _peppered(legacy: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), legacy.encode(), hashlib.sha256).hexdigest()[:32]


def test_pepper_migrates_once_and_the_owner_can_still_reclaim(client, relay, hub, monkeypatch):
    owner = "Owner@Lab.org"
    set_claims(hub, {
        "Lab A": claim_entry(relay, "old-token", email=owner),
        "Lab B": {"email_hash": "", "token_hash": relay._hash("b"), "claimed_at": "2026-04-01"},
    })
    legacy_a = relay._hash(owner)
    monkeypatch.setenv("CLAIMS_PEPPER", "pepper-secret")

    assert client.get("/api/names").json() == {"names": ["Lab A", "Lab B"]}
    assert len(hub.uploads) == 1 and hub.uploads[0]["path"] == CLAIMS
    stored = json.loads(hub.files[CLAIMS])
    assert stored["Lab A"]["email_hash"] == _peppered(legacy_a, "pepper-secret")
    assert stored["Lab A"]["email_hash"] != legacy_a
    assert stored["Lab A"]["v"] == 2
    assert stored["Lab A"]["token_hash"] == relay._hash("old-token"), "tokens keep working"
    assert (stored["Lab B"]["email_hash"], stored["Lab B"]["v"]) == ("", 2)

    # Idempotent, including across a restart: a v2 hash is never HMAC'd twice.
    client.get("/api/names")
    restarted = _load_module(APP_PATH, f"relay_restart_{uuid.uuid4().hex[:8]}")
    try:
        _prepare_relay(restarted, monkeypatch)
        restarted._load_claims()
    finally:
        sys.modules.pop(restarted.__name__, None)
    assert len(hub.uploads) == 1
    assert json.loads(hub.files[CLAIMS])["Lab A"]["email_hash"] == _peppered(legacy_a, "pepper-secret")

    # The owner's original email still passes the "claimed by a different email" check.
    r = client.post("/api/claim-name", json={"pseudonym": "Lab A", "email": "  owner@lab.ORG "})
    assert r.status_code == 200, r.text
    assert relay._pending_codes["Lab A"]["email_hash"] == _peppered(legacy_a, "pepper-secret")
    assert client.post("/api/claim-name",
                       json={"pseudonym": "Lab A", "email": "intruder@else.org"}).status_code == 409

    code = relay._pending_codes["Lab A"]["code"]
    r = client.post("/api/verify-claim", json={"pseudonym": "Lab A", "code": code})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    stored = json.loads(hub.files[CLAIMS])
    assert stored["Lab A"]["v"] == 2
    assert stored["Lab A"]["email_hash"] == _peppered(legacy_a, "pepper-secret")
    assert stored["Lab A"]["token_hash"] == relay._hash(token)
    assert "Lab B" in stored, "a re-claim must not drop other labs"
    assert submit(client, "Lab A", [record()], token=token).json()["verified"] is True


def test_without_pepper_claims_behave_as_before(client, relay, hub):
    set_claims(hub, {"Lab A": claim_entry(relay, "t", email="owner@lab.org")})
    client.get("/api/names")
    assert hub.uploads == [], "no pepper, no rewrite"
    assert client.post("/api/claim-name",
                       json={"pseudonym": "Lab A", "email": "intruder@else.org"}).status_code == 409
    assert client.post("/api/claim-name",
                       json={"pseudonym": "Lab A", "email": "owner@lab.org"}).status_code == 200
    pending = relay._pending_codes["Lab A"]
    assert pending["email_hash"] == relay._hash("owner@lab.org") and "v" not in pending
    r = client.post("/api/verify-claim", json={"pseudonym": "Lab A", "code": pending["code"]})
    assert r.status_code == 200
    assert set(json.loads(hub.files[CLAIMS])["Lab A"]) == {"email_hash", "token_hash", "claimed_at"}


def test_peppered_claim_with_the_secret_missing_is_503_not_a_false_409(client, relay, hub):
    entry = claim_entry(relay, "t", email="owner@lab.org")
    entry.update(email_hash=_peppered(entry["email_hash"], "pepper-secret"), v=2)
    set_claims(hub, {"Lab A": entry})
    r = client.post("/api/claim-name", json={"pseudonym": "Lab A", "email": "owner@lab.org"})
    assert r.status_code == 503


def test_verify_claim_during_an_outage_does_not_wipe_the_claims(client, relay, hub):
    set_claims(hub, {"Lab A": claim_entry(relay, "t"), "Lab B": claim_entry(relay, "u")})
    assert client.post("/api/claim-name",
                       json={"pseudonym": "New Lab", "email": "new@lab.org"}).status_code == 200
    hub.unreachable.add(CLAIMS)
    code = relay._pending_codes["New Lab"]["code"]
    r = client.post("/api/verify-claim", json={"pseudonym": "New Lab", "code": code})
    assert r.status_code == 503
    assert hub.uploads == []


def test_strict_claims_load_never_falls_back_to_a_stale_cached_copy(client, relay, hub, tmp_path, monkeypatch):
    """The REAL hf_hub_download returns the refs/main copy when its HEAD fails.

    That copy can predate this process's own last save, and verify-claim
    would then save a new claim over it, erasing every claim made since.
    """
    import huggingface_hub
    from huggingface_hub import constants, file_download

    cache = tmp_path / "hf_cache"
    storage = cache / ("datasets--" + relay.HF_DATASET_REPO.replace("/", "--"))
    commit = "a" * 40
    (storage / "refs").mkdir(parents=True)
    (storage / "refs" / "main").write_text(commit)
    stale = storage / "snapshots" / commit / CLAIMS
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"Lab A": claim_entry(relay, "t")}))

    def head_times_out(*a, **k):
        raise httpx.ConnectTimeout("HEAD timed out (test)")

    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(file_download, "get_hf_file_metadata", head_times_out)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", file_download.hf_hub_download)
    # Control: this is the fallback being guarded against. huggingface_hub
    # 1.x serves the refs/main copy when the HEAD fails; 2.0 raises instead,
    # so there the hazard is absent and only the relay's behaviour matters.
    try:
        cached = file_download.hf_hub_download(relay.HF_DATASET_REPO, CLAIMS, repo_type="dataset")
    except httpx.ConnectTimeout:
        assert int(huggingface_hub.__version__.split(".")[0]) >= 2
    else:
        assert Path(cached).read_text() == stale.read_text()

    with pytest.raises(Exception):
        relay._load_claims(strict=True)
    relay._pending_codes["New Lab"] = {"code": "123456", "email_hash": relay._hash("n@lab.org"),
                                       "expires": time.time() + 900}
    r = client.post("/api/verify-claim", json={"pseudonym": "New Lab", "code": "123456"})
    assert r.status_code == 503
    assert hub.uploads == []


def test_pepper_migration_cannot_erase_a_claim_saved_meanwhile(client, relay, hub, monkeypatch):
    """A migration that read claims.json before a verify-claim must not save that old read."""
    set_claims(hub, {"Lab A": claim_entry(relay, "t", email="owner@lab.org")})
    stale = relay._fetch_claims()               # read, then the thread is descheduled
    monkeypatch.setenv("CLAIMS_PEPPER", "pepper-secret")
    token = _claim(client, relay, "New Lab", email="new@lab.org")
    view = relay._migrate_claims(stale)         # ...and finishes after New Lab was saved
    assert set(json.loads(hub.files[CLAIMS])) == {"Lab A", "New Lab"}
    assert "New Lab" in view
    assert submit(client, "New Lab", [record()]).status_code == 403
    assert submit(client, "New Lab", [record()], token=token).json()["verified"] is True


# ── no numpy in the Space image ──────────────────────────────────────

_NO_NUMPY_SCRIPT = r"""
import sys
sys.modules["numpy"] = None            # every `import numpy` now raises ImportError
import importlib.util, io
from datetime import datetime, timedelta, timezone
import httpx, huggingface_hub
from huggingface_hub.errors import RemoteEntryNotFoundError

def missing(*a, **k):
    raise RemoteEntryNotFoundError("missing", response=httpx.Response(
        404, request=httpx.Request("GET", "https://huggingface.co/x")))

huggingface_hub.hf_hub_download = missing
spec = importlib.util.spec_from_file_location("relay_no_numpy", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["relay_no_numpy"] = mod
spec.loader.exec_module(mod)
mod._ensure_flush_worker_started = lambda: None
from fastapi.testclient import TestClient
import pyarrow.parquet as pq

c = TestClient(mod.app)
when = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
recs = [{"run_key": f"{i:024x}", "run_date": when, "instrument_family": "timsTOF",
         "instrument_model": "timsTOF HT", "lc_system": "evosep", "spd": 100,
         "peg_intensity_pct": 0.1 * i, "peg_score": 5.0, "peg_n_ions_detected": 1,
         "peg_class": "clean"} for i in range(6)]
r = c.post("/api/peg/submit", json={"display_name": "Lab N", "records": recs})
assert r.status_code == 200 and r.json()["accepted"] == 6, r.text
for url in ("/api/peg/leaderboard", "/api/peg/trend", "/api/peg/lc-compare"):
    r = c.get(url)
    assert r.status_code == 200, (url, r.text)
assert c.get("/api/peg/leaderboard").json()["ranked"][0]["n_runs"] == 6
for it in list(mod._SUBMIT_QUEUE.queue):
    assert pq.read_table(io.BytesIO(it.data)).num_rows == 6
print("NO-NUMPY-OK", flush=True)
# Skip interpreter teardown: on macOS + pyarrow 24 the process can hang for
# good in arrow's global ThreadPool destructor at exit (seen in `sample`:
# ~ThreadPool -> Shutdown -> pthread_cond_wait). The checks above are done.
import os
os._exit(0)
"""


def test_peg_channel_runs_without_numpy():
    """The Space's Dockerfile installs no numpy; the PEG path must not need it."""
    proc = subprocess.run(
        [sys.executable, "-c", _NO_NUMPY_SCRIPT, str(APP_PATH)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "NO-NUMPY-OK" in proc.stdout


def test_dockerfile_still_installs_no_numpy():
    assert "numpy" not in (REPO / "hf_space" / "Dockerfile").read_text()


# ── scripts/deploy_hf_space.py ───────────────────────────────────────

BASE_TEXT = 'SPACE_VERSION = "1.1.0"\nprint("base")\n'
LOCAL_TEXT = 'SPACE_VERSION = "1.2.0"\nprint("base")\nprint("peg")\n'
DOCKER_BASE = "RUN pip install huggingface_hub\n"
DOCKER_LOCAL = "RUN pip install huggingface_hub==2.0.0\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def deploy(tmp_path, monkeypatch):
    name = f"deploy_under_test_{uuid.uuid4().hex[:8]}"
    mod = _load_module(DEPLOY_PATH, name)
    local = tmp_path / "hf_space" / "app.py"
    local.parent.mkdir()
    local.write_text(LOCAL_TEXT)
    script_copy = tmp_path / "deploy_copy.py"
    script_copy.write_text(DEPLOY_PATH.read_text())
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "LOCAL_APP", local)
    monkeypatch.setattr(mod, "SCRIPT_PATH", script_copy)
    monkeypatch.setattr(mod, "RECORDED_BASE_SHA256", _sha(BASE_TEXT))
    dockerfile = local.parent / "Dockerfile"
    dockerfile.write_text(DOCKER_LOCAL)
    monkeypatch.setattr(mod, "LOCAL_DOCKERFILE", dockerfile)
    monkeypatch.setattr(mod, "RECORDED_DOCKERFILE_SHA256", _sha(DOCKER_BASE))
    # Default: the Space already has the local Dockerfile, so only app.py moves.
    monkeypatch.setattr(mod, "fetch_live_file", lambda repo_id, commit, name: DOCKER_LOCAL.encode())
    monkeypatch.setattr(mod, "in_sync_window", lambda now: False)
    calls: dict[str, list] = {"upload": [], "wait": []}

    def fake_upload(local_path, repo_id, parent_commit, version, extra=None):
        call = {"path": local_path, "parent": parent_commit, "version": version}
        if extra:
            call["extra"] = extra
        calls["upload"].append(call)
        return "https://huggingface.co/spaces/brettsp/stan/commit/new"

    def fake_wait(url, expected, timeout_sec, interval_sec=15.0):
        calls["wait"].append(expected)
        return True

    def forbid_network(*a, **k):
        raise AssertionError("no network in tests")

    monkeypatch.setattr(mod, "upload", fake_upload)
    monkeypatch.setattr(mod, "wait_for_version", fake_wait)
    monkeypatch.setattr(mod, "report_health", lambda url: None)
    monkeypatch.setattr(mod, "get_json", forbid_network)
    mod.calls = calls
    mod.script_copy = script_copy
    yield mod
    sys.modules.pop(name, None)


def _live(deploy, monkeypatch, text: str, commit: str = "d041ef68aa") -> None:
    monkeypatch.setattr(deploy, "fetch_live", lambda repo_id=None: (commit, text.encode()))


def test_deploy_dry_run_by_default(deploy, monkeypatch):
    _live(deploy, monkeypatch, BASE_TEXT)
    assert deploy.main([]) == 0
    assert deploy.calls["upload"] == []


def test_deploy_refuses_when_the_space_was_edited(deploy, monkeypatch):
    _live(deploy, monkeypatch, BASE_TEXT + "# hotfix made in the Space\n")
    assert deploy.main(["--yes"]) == 3
    assert deploy.calls["upload"] == []


def test_deploy_uploads_against_the_checked_commit(deploy, monkeypatch):
    _live(deploy, monkeypatch, BASE_TEXT, commit="d041ef68aa")
    assert deploy.main(["--yes"]) == 0
    assert deploy.calls["upload"] == [{"path": deploy.LOCAL_APP, "parent": "d041ef68aa", "version": "1.2.0"}]
    assert deploy.calls["wait"] == ["1.2.0"]
    assert deploy.script_copy.read_text() == DEPLOY_PATH.read_text(), "no --record-base, no rewrite"


def test_deploy_record_base_rewrites_the_constant(deploy, monkeypatch):
    _live(deploy, monkeypatch, BASE_TEXT)
    assert deploy.main(["--yes", "--record-base"]) == 0
    text = deploy.script_copy.read_text()
    assert re.findall(r'^RECORDED_BASE_SHA256 = "([0-9a-f]{64})"$', text, re.MULTILINE) == [_sha(LOCAL_TEXT)]


def test_deploy_already_deployed_is_a_no_op(deploy, monkeypatch):
    _live(deploy, monkeypatch, LOCAL_TEXT)
    assert deploy.main(["--yes"]) == 0
    assert deploy.calls["upload"] == []


def test_deploy_refuses_an_unbumped_version(deploy, monkeypatch):
    deploy.LOCAL_APP.write_text(BASE_TEXT + "print('changed')\n")
    _live(deploy, monkeypatch, BASE_TEXT)
    assert deploy.main(["--yes"]) == 4
    assert deploy.calls["upload"] == []


def test_deploy_refuses_inside_the_sync_window(deploy, monkeypatch):
    _live(deploy, monkeypatch, BASE_TEXT)
    monkeypatch.setattr(deploy, "in_sync_window", lambda now: True)
    assert deploy.main(["--yes"]) == 5
    assert deploy.main(["--yes", "--ignore-sync-window"]) == 0


def test_deploy_reports_a_space_that_never_comes_up(deploy, monkeypatch):
    _live(deploy, monkeypatch, BASE_TEXT)
    monkeypatch.setattr(deploy, "wait_for_version", lambda *a, **k: False)
    assert deploy.main(["--yes", "--record-base"]) == 6
    assert _sha(LOCAL_TEXT) not in deploy.script_copy.read_text(), "an unverified deploy is not recorded"


def _live_dockerfile(deploy, monkeypatch, text: str) -> None:
    monkeypatch.setattr(deploy, "fetch_live_file", lambda repo_id, commit, name: text.encode())


def test_deploy_ships_the_pinned_dockerfile_in_the_same_commit(deploy, monkeypatch):
    """The unpinned base Dockerfile would rebuild with huggingface_hub 2.0 and crash the relay."""
    _live(deploy, monkeypatch, BASE_TEXT)
    _live_dockerfile(deploy, monkeypatch, DOCKER_BASE)
    assert deploy.main(["--yes"]) == 0
    assert deploy.calls["upload"] == [{"path": deploy.LOCAL_APP, "parent": "d041ef68aa", "version": "1.2.0",
                                       "extra": {"Dockerfile": deploy.LOCAL_DOCKERFILE}}]


def test_deploy_refuses_a_dockerfile_edited_in_the_space(deploy, monkeypatch):
    _live(deploy, monkeypatch, BASE_TEXT)
    _live_dockerfile(deploy, monkeypatch, DOCKER_BASE + "RUN pip install numpy\n")
    assert deploy.main(["--yes"]) == 3
    assert deploy.calls["upload"] == []


def test_deploy_record_base_records_the_dockerfile_too(deploy, monkeypatch):
    _live(deploy, monkeypatch, BASE_TEXT)
    _live_dockerfile(deploy, monkeypatch, DOCKER_BASE)
    assert deploy.main(["--yes", "--record-base"]) == 0
    text = deploy.script_copy.read_text()
    assert re.findall(r'^RECORDED_BASE_SHA256 = "([0-9a-f]{64})"$', text, re.MULTILINE) == [_sha(LOCAL_TEXT)]
    assert re.findall(r'^RECORDED_DOCKERFILE_SHA256 = "([0-9a-f]{64})"$', text, re.MULTILINE) == [_sha(DOCKER_LOCAL)]


def test_vendored_dockerfile_pins_every_package():
    """An unpinned install is how a new huggingface_hub release could take the relay down."""
    text = (REPO / "hf_space" / "Dockerfile").read_text()
    pkgs = re.findall(r"^\s+([A-Za-z_\[\]]+(?:==[0-9][^\s\\]*)?)\s*\\?$", text, re.MULTILINE)
    assert pkgs and all("==" in p for p in pkgs), pkgs


def test_deploy_helpers(deploy, tmp_path):
    assert deploy.classify("a", "a", "b") == "deployed"
    assert deploy.classify("b", "a", "b") == "base"
    assert deploy.classify("c", "a", "b") == "drift"
    assert deploy.diff_stat("a\nb\nc\n", "a\nB\nc\nd\n") == (2, 1, 2)
    assert deploy.space_version_of(LOCAL_TEXT) == "1.2.0"
    real = _load_module(DEPLOY_PATH, f"deploy_plain_{uuid.uuid4().hex[:8]}")
    try:
        assert real.in_sync_window(datetime(2026, 9, 28, 7, 27, tzinfo=timezone.utc))
        assert not real.in_sync_window(datetime(2026, 9, 28, 7, 50, tzinfo=timezone.utc))
        assert not real.in_sync_window(datetime(2026, 9, 28, 8, 27, tzinfo=timezone.utc))
        with pytest.raises(ValueError):
            real.rewrite_recorded_base(deploy.script_copy, "not-a-sha")
        assert real.LOCAL_APP == APP_PATH
        assert re.fullmatch(r"[0-9a-f]{64}", real.RECORDED_BASE_SHA256)
    finally:
        sys.modules.pop(real.__name__, None)


@pytest.fixture
def real_deploy():
    mod = _load_module(DEPLOY_PATH, f"deploy_real_{uuid.uuid4().hex[:8]}")
    yield mod
    sys.modules.pop(mod.__name__, None)


PACIFIC = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize("day", ["2026-09-28", "2026-11-15", "2027-01-15"])   # PDT, PST, PST
@pytest.mark.parametrize("hhmm", ["00:27", "06:27", "12:27", "18:27"])
def test_sync_window_follows_hive_local_time_through_dst(real_deploy, day, hhmm):
    """Hive's crontab ("25 */6 * * *") runs in America/Los_Angeles, so in UTC the sync moves by an hour in winter."""
    t = datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=PACIFIC)
    assert real_deploy.in_sync_window(t)
    assert real_deploy.in_sync_window(t.astimezone(timezone.utc))
    assert not real_deploy.in_sync_window(t.replace(hour=3))
    assert not real_deploy.in_sync_window(t.replace(minute=50))


def test_sync_window_without_a_tz_database_guards_both_offsets(real_deploy, monkeypatch):
    def no_tzdata(key):
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(real_deploy, "ZoneInfo", no_tzdata)
    summer = datetime(2026, 9, 28, 7, 27, tzinfo=timezone.utc)    # 00:27 PDT
    winter = datetime(2026, 11, 15, 8, 27, tzinfo=timezone.utc)   # 00:27 PST
    assert real_deploy.in_sync_window(summer) and real_deploy.in_sync_window(winter)
    assert not real_deploy.in_sync_window(datetime(2026, 11, 15, 11, 27, tzinfo=timezone.utc))


# ── public page: the "Evosep PEG Watch" section of INDEX_HTML ────────
# The section's HTML builders are plain functions of the /api/peg/* payloads
# (the <script id="peg-watch-js"> block) so they can run in node against a
# real relay payload and against hostile strings. Lab names are
# submitter-chosen and unauthenticated until claimed: every one must be
# escaped, and the page must not depend on the benchmark rows at all.

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
EVIL = '<img src=x onerror="alert(1)">'

_PAGE_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const calls = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
// No document or window: the block must define its builders without
// touching the DOM, and skip its own auto-start.
const ctx = vm.createContext({ console });
vm.runInContext(src, ctx);
process.stdout.write(JSON.stringify(calls.map(([fn, args]) => ctx[fn](...args))));
"""


def _page(client) -> str:
    r = client.get("/")
    assert r.status_code == 200
    return r.text


def _script_block(html: str, block_id: str) -> str:
    m = re.search(rf'<script id="{block_id}">(.*?)</script>', html, re.S)
    assert m, f"no <script id={block_id}> in the page"
    return m.group(1)


def run_page_js(tmp_path: Path, html: str, calls: list) -> list:
    """Run [(builder, args), ...] from the page's PEG script in node; return the results."""
    src = tmp_path / "peg_page.js"
    src.write_text(_script_block(html, "stan-esc") + "\n" + _script_block(html, "peg-watch-js"))
    harness = tmp_path / "harness.js"
    harness.write_text(_PAGE_HARNESS)
    args = tmp_path / "calls.json"
    args.write_text(json.dumps(calls))
    proc = subprocess.run([NODE, str(harness), str(src), str(args)],
                          capture_output=True, text=True, timeout=60, check=True)
    return json.loads(proc.stdout)


def test_page_has_the_peg_section_linked_from_the_header(client):
    html = _page(client)
    assert "community site v1.9.0" in html
    assert '<a href="#peg">PEG Watch</a>' in html
    assert html.count('id="peg"') == 1
    for endpoint in ("/api/peg/leaderboard", "/api/peg/trend", "/api/peg/lc-compare"):
        assert endpoint in html
    for needle in ("Evosep PEG Watch", "Evosep vs other LC", "not like-for-like",
                   "<code>peg_share: true</code>", "<code>~/.stan/community.yml</code>",
                   "<code>stan peg-sync</code>", "<code>stan community-claim</code>",
                   "https://github.com/bsphinney/stan/blob/main/docs/PEG_EVOSEP_DIAGNOSTIC.md"):
        assert needle in html, needle
    assert 'data-win="30"' in html and 'data-win="90"' in html and 'data-win="365"' in html
    # showTab() strips .active from every .tab on the page; the PEG toggles must not be .tab.
    peg = html[html.index('<div class="section" id="peg">'):html.index("<!-- Community Submissions -->")]
    assert 'class="tab' not in peg


@needs_node
def test_every_page_script_parses(client, tmp_path):
    blocks = [b for b in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", _page(client), re.S) if b.strip()]
    assert len(blocks) >= 3
    for i, block in enumerate(blocks):
        path = tmp_path / f"block_{i}.js"
        path.write_text(block)
        proc = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f"script block {i}: {proc.stderr}"


@needs_node
def test_board_renders_the_relay_payload_in_rank_order(client, relay, hub, tmp_path):
    _seed_board(client, relay, hub)
    board = client.get("/api/peg/leaderboard", params={"family": "timsTOF", "spd": 100, "window": 30}).json()
    [parts, chips] = run_page_js(tmp_path, _page(client), [
        ["pegBoardParts", [board]],
        ["pegCohortChipsHtml", [board["cohorts"], "timsTOF", 100]],
    ])
    rows = re.findall(r"<tr>.*?</tr>", parts["main"].split("<tbody>", 1)[1], re.S)
    assert [re.search(r'class="peg-lab">(.*?)<', r).group(1) for r in rows] == ["Alpha", "Delta", "Bravo"]
    alpha, delta, bravo = rows
    assert "peg-ok" in alpha and "unverified" not in alpha, "Alpha is verified"
    assert "unverified" in delta and "peg-ok" not in delta
    assert "Cleanest" in alpha and "Most improved" in alpha
    assert "Cleanest" not in delta + bravo and "Most improved" not in delta + bravo
    assert "▼ 75%" in alpha, "change -75 renders as a fall"
    assert '<span class="peg-muted">—</span></td></tr>' in bravo, "no previous window: blank change"
    assert "0.5%" in alpha and "78%" in alpha and ">9<" in alpha
    assert "Charlie" in parts["unranked"] and "3 runs" in parts["unranked"]
    assert "Echo" not in parts["main"] + parts["unranked"], "other-LC labs are never on the board"
    assert "4 labs" in parts["community"] and "23 QC runs" in parts["community"]
    assert chips.count("<button") == 2
    assert re.search(r'aria-pressed="true"[^>]*>timsTOF · 100 SPD<', chips)
    assert re.search(r'aria-pressed="false"[^>]*>timsTOF · 60 SPD<', chips)


@needs_node
def test_builders_escape_every_relay_string(client, tmp_path):
    board = {
        "window_days": 30, "family": EVIL, "spd": 100,
        "cohorts": [{"family": EVIL, "spd": 100, "n_labs": 2, "n_runs_365d": 40}],
        "ranked": [{"rank": 1, "display_name": EVIL, "verified": False, "instrument_models": [EVIL, "timsTOF HT"],
                    "n_runs": 9, "median_pct": 0.5, "clean_pct": 78, "heavy_pct": 0, "change_pct": None,
                    "weekly": [0.4, None, 0.5], "badges": []}],
        "unranked": [{"display_name": EVIL, "verified": False, "n_runs": 2}],
        "community": {"n_labs": 2, "n_runs": 11, "p25_pct": 0.1, "median_pct": 0.5, "p75_pct": 1.0},
    }
    lc = {"family": EVIL, "window_days": 90, "as_of": "2026-09-28", "groups": [
        {"lc": "evosep", "n_labs": 1, "n_runs": 20, "p25_pct": 0.2, "median_pct": 1.0, "p75_pct": 3.0,
         "clean_pct": 60, "heavy_pct": 10, "weekly": [1.0, 2.0]},
        {"lc": "other", "n_labs": 1, "n_runs": 8, "p25_pct": 0.1, "median_pct": 0.3, "p75_pct": 0.6,
         "clean_pct": 90, "heavy_pct": 0, "weekly": [0.3, 0.2]},
    ], "families": [{"family": EVIL, "evosep_runs": 20, "other_runs": 8, "evosep_labs": 1, "other_labs": 1}]}
    # One LC only: the populated card and the placeholder both carry the family name.
    lc_one = dict(lc, groups=[lc["groups"][0], _lc_group("other", 0)])
    out = run_page_js(tmp_path, _page(client), [
        ["pegBoardParts", [board]],
        ["pegCohortChipsHtml", [board["cohorts"], EVIL, 100]],
        ["pegLcHtml", [lc]],
        ["pegLcFamilyChipsHtml", [lc["families"], EVIL]],
        ["esc", [EVIL]],
        ["pegLcHtml", [lc_one]],
    ])
    parts, chips, lc_html, fam_chips, escaped, lc_one_html = out
    assert escaped == "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"
    assert "peg-lcg-empty" in lc_one_html and lc_one_html.count("&lt;img") >= 3
    for html in (parts["main"], parts["unranked"], parts["community"], chips, lc_html, fam_chips, lc_one_html):
        assert "<img" not in html and "onerror=\"" not in html
        assert "&lt;img" in html


def _lc_group(lc: str, n_runs: int, n_labs: int = 0, weekly: list | None = None, **pcts) -> dict:
    """One /api/peg/lc-compare group; with no runs, the relay's nulls (as served live)."""
    g = {"lc": lc, "n_labs": n_labs, "n_runs": n_runs, "p25_pct": None, "median_pct": None, "p75_pct": None,
         "clean_pct": None, "heavy_pct": None, "weekly": weekly if weekly is not None else [None] * 26}
    g.update(pcts)
    return g


def _lc_slots(html: str) -> tuple[list[str], str]:
    """(the two LC slot cards in order, the fine print under them)."""
    body, fine = html.split('<p class="peg-fine">', 1)
    starts = [m.start() for m in re.finditer(r'<div class="peg-lcg(?: peg-lcg-empty)?">', body)]
    return [body[a:b] for a, b in zip(starts, starts[1:] + [len(body)])], fine


@needs_node
def test_lc_panel_shows_each_lc_with_data_and_a_placeholder_for_the_other(client, relay, hub, tmp_path):
    """A family with one LC so far shows that LC's card, never an empty box (live: UC Davis
    shares Evosep only on timsTOF and its own LC only on the Orbitraps)."""
    _seed_board(client, relay, hub)
    timstof = client.get("/api/peg/lc-compare", params={"family": "timsTOF"}).json()
    orbitrap = client.get("/api/peg/lc-compare", params={"family": "Orbitrap"}).json()
    # The live timsTOF payload's shape: Evosep only, other LC all zeros and nulls.
    families = [{"family": "timsTOF", "evosep_runs": 179, "other_runs": 0, "evosep_labs": 1, "other_labs": 0},
                {"family": "Exploris", "evosep_runs": 0, "other_runs": 95, "evosep_labs": 0, "other_labs": 1}]
    evosep_only = {"family": "timsTOF", "window_days": 90, "families": families, "groups": [
        _lc_group("evosep", 179, 1, weekly=[4.0 + k / 10 for k in range(26)],
                  p25_pct=2.1, median_pct=4.58, p75_pct=8.0, clean_pct=12, heavy_pct=30),
        _lc_group("other", 0)]}
    # Other-LC runs 4 months ago but none in the 90-day window: not "no lab yet".
    lapsed = dict(evosep_only, groups=[evosep_only["groups"][0], _lc_group("other", 0, weekly=[None, 0.3] + [None] * 24)])
    empty = {"family": "Astral", "window_days": 90, "groups": [], "families": []}
    both, orbi, tims, lapsed_html, none_ = run_page_js(tmp_path, _page(client), [
        ["pegLcHtml", [timstof]], ["pegLcHtml", [orbitrap]], ["pegLcHtml", [evosep_only]],
        ["pegLcHtml", [lapsed]], ["pegLcHtml", [empty]],
    ])

    # Both LCs: two data cards on one weekly scale, no placeholder.
    assert both.count('class="peg-lcg"') == 2 and "peg-lcg-empty" not in both
    assert "EVOSEP" in both.upper() and "OTHER LC" in both.upper()
    assert "share one scale" in both

    # Orbitrap, other LC only: an Evosep placeholder first, then the full other-LC card.
    (evo_slot, oth_slot), fine = _lc_slots(orbi)
    assert evo_slot.startswith('<div class="peg-lcg peg-lcg-empty">')
    assert ">Evosep<" in evo_slot and "no lab yet" in evo_slot
    assert "Labs running Orbitrap on an Evosep can join: <code>stan peg-sync</code>" in evo_slot
    assert '<a href="#peg-join">' in evo_slot
    for data_only in ("peg-big", "peg-iqr", "peg-spark", "median PEG share"):
        assert data_only not in evo_slot, data_only
    assert oth_slot.startswith('<div class="peg-lcg">') and ">Other LC<" in oth_slot
    assert "1 lab · 7 runs" in oth_slot and "0.2%<small>median PEG share</small>" in oth_slot
    for part in ('class="peg-iqr"', "peg-lcg-meta", "clean <b>", 'class="peg-spark', "weekly median, last 26 weeks"):
        assert part in oth_slot, part
    assert "Only Orbitrap labs are compared here; the Evosep side fills in as Orbitrap labs on an Evosep join." in fine
    assert "share one scale" not in fine
    assert "appears once" not in orbi, "the old wait-for-both note is gone"

    # timsTOF, Evosep only (the live case): the Evosep card, then an other-LC placeholder.
    (evo_slot, oth_slot), fine = _lc_slots(tims)
    assert evo_slot.startswith('<div class="peg-lcg">') and "1 lab · 179 runs" in evo_slot
    assert "4.6%<small>median PEG share</small>" in evo_slot and "clean <b>12%</b>" in evo_slot
    assert oth_slot.startswith('<div class="peg-lcg peg-lcg-empty">') and ">Other LC<" in oth_slot
    assert "no lab yet" in oth_slot
    assert "Labs running timsTOF with a nanoElute or other LC can join: <code>stan peg-sync</code>" in oth_slot
    assert "Only timsTOF labs are compared here; the Other LC side fills in as timsTOF labs on a non-Evosep LC join." in fine
    # Never a cross-family comparison: the other families in the payload are not drawn.
    assert "Exploris" not in tims and "timsTOF" not in orbi
    assert all(len(_lc_slots(h)[0]) == 2 for h in (both, orbi, tims, lapsed_html)), "always exactly two slots"

    # Older runs outside the window: the placeholder says "none lately", not "no lab yet".
    (_, oth_slot), _ = _lc_slots(lapsed_html)
    assert "none in the last 90 days" in oth_slot and "no lab yet" not in oth_slot
    assert "No timsTOF lab on a non-Evosep LC has shared PEG in the last 90 days." in oth_slot

    # No runs at all in the family: one empty state, no cards.
    assert "peg-lcg" not in none_ and "No Astral lab has shared PEG" in none_


@needs_node
def test_trend_band_breaks_at_empty_weeks(client, tmp_path):
    def wk(day, n, p25=None, p50=None, p75=None):
        return {"week_start": f"2026-09-{day:02d}", "n_labs": 1 if n else 0, "n_runs": n,
                "p25": p25, "p50": p50, "p75": p75}
    weeks = [wk(1, 3, 0.1, 0.2, 0.4), wk(8, 4, 0.2, 0.3, 0.5), wk(15, 0),
             wk(22, 2, 0.3, 0.4, 0.6), wk(29, 5, 0.1, 0.5, 0.9)]
    [tr, empty] = run_page_js(tmp_path, _page(client), [
        ["pegTrendTraces", [weeks]], ["pegTrendTraces", [[wk(1, 0), wk(8, 0)]]],
    ])
    bands = [t for t in tr["traces"] if t.get("fill") == "toself"]
    assert len(bands) == 2, "the empty week splits the band instead of being bridged"
    assert bands[0]["x"] == ["2026-09-01", "2026-09-08", "2026-09-08", "2026-09-01"]
    assert bands[0]["y"] == [0.4, 0.5, 0.2, 0.1]
    assert [b["showlegend"] for b in bands] == [True, False]
    median = tr["traces"][-1]
    assert median["y"] == [0.2, 0.3, None, 0.4, 0.5] and median["connectgaps"] is False
    assert (tr["nWeeks"], tr["maxLabs"]) == (4, 1)
    assert (empty["nWeeks"], empty["traces"][-1]["name"]) == (0, "median")


def test_join_card_puts_claiming_before_syncing(client):
    """Syncing under an unclaimed name first lets anyone claim it and take the lab's board entry."""
    html = _page(client)
    card = html[html.index('id="peg-join"'):]
    steps = card[:card.index("</ol>")]
    assert steps.index("stan community-claim") < steps.index("peg_share: true") < steps.index("stan peg-sync")
    assert "claimed by anyone" in steps


_MAIN_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const data = fs.readFileSync(process.argv[3], 'utf8');
const el = () => ({ style: {}, innerHTML: '', textContent: '', classList: { add() {}, remove() {} },
                    addEventListener() {}, querySelector: () => null, querySelectorAll: () => [] });
const plots = [];
const ctx = vm.createContext({
    console,
    document: { getElementById: el, querySelector: () => null, querySelectorAll: () => [],
                addEventListener() {}, createElement: el, body: el() },
    window: { addEventListener() {} },
    fetch: () => new Promise(() => {}),       // the page's own load never settles here
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0,
    Plotly: { newPlot: (id, traces) => plots.push({ id, traces }), Plots: { resize() {} } },
});
vm.runInContext(src, ctx);
vm.runInContext(`allData = ${data}; renderColumnComparison();`, ctx);
process.stdout.write(JSON.stringify(plots));
process.exit(0);
"""


@needs_node
def test_column_comparison_renders_when_two_columns_share_a_cohort(client, tmp_path):
    """readableCohort lived inside renderRefRanges; this chart threw a ReferenceError instead."""
    html = _page(client)
    main = next(b for b in re.findall(r"<script>(.*?)</script>", html, re.S) if "function renderColumnComparison" in b)
    subs = [{"cohort_id": "timsTOF_100spd_low", "instrument_model": "timsTOF HT", "instrument_family": "timsTOF",
             "acquisition_mode": "dia", "n_precursors": n, "column_vendor": "PepSep", "column_model": col,
             "amount_ng": 50, "spd": 100}
            for n, col in [(40000, "15 cm"), (42000, "15 cm"), (45000, "25 cm"), (47000, "25 cm")]]
    # esc() lives in its own block, loaded before this one on the page.
    (tmp_path / "main.js").write_text(_script_block(html, "stan-esc") + "\n" + main)
    (tmp_path / "data.json").write_text(json.dumps(subs))
    (tmp_path / "harness.js").write_text(_MAIN_HARNESS)
    proc = subprocess.run([NODE, str(tmp_path / "harness.js"), str(tmp_path / "main.js"), str(tmp_path / "data.json")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    [plot] = json.loads(proc.stdout)
    assert plot["id"] == "chart-column-compare"
    # The cohort label carries the track: DIA and DDA never share a bar (P1, D1).
    # P2a: the label says the SPD and amount the runs carry, not the tier name.
    # P2b: the gradient is named as the page's cohort key names it (B2); these
    # rows record no LC at an Evosep-method SPD, so that is said, not inferred.
    assert [t["x"] for t in plot["traces"]] == [["timsTOF HT · DIA<br>100 SPD, LC not recorded · 50 ng"]] * 2
    assert sorted(t["y"][0] for t in plot["traces"]) == [41000, 46000]
