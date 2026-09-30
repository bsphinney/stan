"""Tests for the setup wizard data structures and helpers.

Catches integration bugs like:
- LC_METHODS missing from stan.setup
- generate_pseudonym format issues
- is_pseudonym failing to identify generated names
"""

from __future__ import annotations


# ── 1. LC_METHODS structure ──────────────────────────────────────


def test_lc_methods_is_list() -> None:
    """LC_METHODS must be a list, not a dict or other type."""
    from stan.setup import LC_METHODS

    assert isinstance(LC_METHODS, list)


def test_lc_methods_required_keys() -> None:
    """Every LC method entry must have name, spd, and gradient_min."""
    from stan.setup import LC_METHODS

    for i, method in enumerate(LC_METHODS):
        assert isinstance(method, dict), f"LC_METHODS[{i}] is not a dict"
        assert "name" in method, f"LC_METHODS[{i}] missing 'name'"
        assert "spd" in method, f"LC_METHODS[{i}] missing 'spd'"
        assert "gradient_min" in method, f"LC_METHODS[{i}] missing 'gradient_min'"


def test_lc_methods_spd_values_are_ints() -> None:
    """SPD values must be integers for downstream math."""
    from stan.setup import LC_METHODS

    for method in LC_METHODS:
        assert isinstance(method["spd"], int), (
            f"'{method['name']}' has spd={method['spd']!r} which is not int"
        )


def test_lc_methods_gradient_min_types() -> None:
    """gradient_min must be int/float for real methods, or None for custom."""
    from stan.setup import LC_METHODS

    for method in LC_METHODS:
        if method["spd"] > 0:
            assert isinstance(method["gradient_min"], (int, float)), (
                f"'{method['name']}' has gradient_min={method['gradient_min']!r} "
                f"but spd > 0 — should be numeric"
            )
        # spd == 0 entries can have gradient_min = None (custom)


def test_lc_methods_names_are_strings() -> None:
    """Method names should be non-empty strings."""
    from stan.setup import LC_METHODS

    for method in LC_METHODS:
        assert isinstance(method["name"], str)
        assert len(method["name"]) > 0


# ── 2. generate_pseudonym ────────────────────────────────────────


def test_generate_pseudonym_format() -> None:
    """generate_pseudonym should return 'Adjective Scientist' format."""
    from stan.community.pseudonym import generate_pseudonym

    name = generate_pseudonym()
    parts = name.split()
    assert len(parts) == 2, f"Expected 'Adjective Scientist', got {name!r}"
    assert parts[0][0].isupper(), f"Adjective should be capitalized: {name!r}"
    assert parts[1][0].isupper(), f"Scientist should be capitalized: {name!r}"


def test_generate_pseudonym_deterministic_with_seed() -> None:
    """Same seed should produce the same pseudonym."""
    from stan.community.pseudonym import generate_pseudonym

    name1 = generate_pseudonym(seed="test-lab-123")
    name2 = generate_pseudonym(seed="test-lab-123")
    assert name1 == name2

    # Different seed should (very likely) produce a different name
    name3 = generate_pseudonym(seed="other-lab-456")
    # Not guaranteed different, but with 2500 combos it almost certainly is
    # We don't assert inequality to avoid flaky tests


def test_generate_pseudonym_uses_word_lists() -> None:
    """Generated names should use words from the defined word lists."""
    from stan.community.pseudonym import ADJECTIVES, SCIENTISTS, generate_pseudonym

    for _ in range(20):
        name = generate_pseudonym()
        adj, sci = name.split()
        assert adj in ADJECTIVES, f"Adjective {adj!r} not in ADJECTIVES list"
        assert sci in SCIENTISTS, f"Scientist {sci!r} not in SCIENTISTS list"


# ── 3. is_pseudonym ──────────────────────────────────────────────


def test_is_pseudonym_identifies_generated_names() -> None:
    """is_pseudonym should return True for names from generate_pseudonym."""
    from stan.community.pseudonym import generate_pseudonym, is_pseudonym

    for _ in range(10):
        name = generate_pseudonym()
        assert is_pseudonym(name), f"is_pseudonym should recognize {name!r}"


def test_is_pseudonym_rejects_real_names() -> None:
    """is_pseudonym should return False for names not in the word lists."""
    from stan.community.pseudonym import is_pseudonym

    assert is_pseudonym("Brett Phinney") is False
    assert is_pseudonym("Anonymous Lab") is False
    assert is_pseudonym("UC Davis Proteomics") is False


def test_is_pseudonym_rejects_single_words() -> None:
    """is_pseudonym requires exactly two words."""
    from stan.community.pseudonym import is_pseudonym

    assert is_pseudonym("Nimble") is False
    assert is_pseudonym("") is False
    assert is_pseudonym("Nimble Edman Extra") is False


def test_is_pseudonym_case_sensitive() -> None:
    """is_pseudonym should be case-sensitive (word lists are title case)."""
    from stan.community.pseudonym import is_pseudonym

    # These are valid words but wrong case
    assert is_pseudonym("nimble edman") is False
    assert is_pseudonym("NIMBLE EDMAN") is False


# ── 4. Word list sanity ──────────────────────────────────────────


def test_word_lists_not_empty() -> None:
    """Both word lists must have entries for pseudonym generation to work."""
    from stan.community.pseudonym import ADJECTIVES, SCIENTISTS

    assert len(ADJECTIVES) >= 10, "ADJECTIVES list is suspiciously short"
    assert len(SCIENTISTS) >= 10, "SCIENTISTS list is suspiciously short"


def test_word_lists_no_duplicates() -> None:
    """Word lists should not contain duplicates."""
    from stan.community.pseudonym import ADJECTIVES, SCIENTISTS

    assert len(ADJECTIVES) == len(set(ADJECTIVES)), "ADJECTIVES has duplicates"
    assert len(SCIENTISTS) == len(set(SCIENTISTS)), "SCIENTISTS has duplicates"


def test_word_lists_title_case() -> None:
    """All words should be title case for consistent display."""
    from stan.community.pseudonym import ADJECTIVES, SCIENTISTS

    for word in ADJECTIVES:
        assert word[0].isupper(), f"ADJECTIVE {word!r} is not title case"
    for word in SCIENTISTS:
        assert word[0].isupper(), f"SCIENTIST {word!r} is not title case"


# ── 5. Email verification echoes the relay's claim_id ─────────────────

class _Resp:
    def __init__(self, body: dict) -> None:
        self._body = body

    def read(self) -> bytes:
        import json
        return json.dumps(self._body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _run_verification(monkeypatch, claim_answer: dict) -> list[dict]:
    """Drive _verify_name_ownership against a fake relay; return the JSON bodies posted."""
    import json
    import urllib.request

    import stan.setup as stan_setup

    answers = iter(["owner@lab.org", "123456"])
    monkeypatch.setattr(stan_setup.Prompt, "ask", lambda *a, **k: next(answers))
    posted: list[dict] = []

    def fake_urlopen(req, timeout=None):
        posted.append(json.loads(req.data))
        if req.full_url.endswith("/api/claim-name"):
            return _Resp(claim_answer)
        return _Resp({"status": "verified", "token": "tok-new"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert stan_setup._verify_name_ownership("Clogged PeakTail") == "tok-new"
    return posted


def test_verification_echoes_the_claim_id(monkeypatch) -> None:
    """The relay binds a code's wrong-guess budget to the claim_id, so a
    stranger cannot throw away the code the owner was just emailed."""
    posted = _run_verification(monkeypatch, {"status": "code_sent", "claim_id": "cid-Zq9"})
    assert posted[1] == {"pseudonym": "Clogged PeakTail", "code": "123456", "claim_id": "cid-Zq9"}


def test_verification_without_a_claim_id_sends_none(monkeypatch) -> None:
    """A relay that predates claim_id still gets the request it expects."""
    posted = _run_verification(monkeypatch, {"status": "code_sent"})
    assert posted[1] == {"pseudonym": "Clogged PeakTail", "code": "123456"}


# ── 6. Shared config fixtures (temp HOME, never the real ~/.stan) ─────

import logging  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
import yaml  # noqa: E402


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch) -> Path:
    """Point STAN's user config dir at a temp HOME for the whole test."""
    import stan.config as cfg

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    user = home / ".stan"
    monkeypatch.setattr(cfg, "_USER_CONFIG_DIR", user)
    monkeypatch.setattr(cfg, "_LEGACY_SUBMIT_NOTED", False)
    return user


def _yml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ── 7. Minimal config files (what `stan init` creates) ────────────────


def test_ensure_default_configs_creates_three_parseable_files(cfg_dir) -> None:
    from stan.setup import ensure_default_configs

    results = ensure_default_configs()
    assert [(p.name, created) for p, created in results] == [
        ("instruments.yml", True), ("thresholds.yml", True), ("community.yml", True),
    ]
    assert _yml(cfg_dir / "instruments.yml") == {"instruments": []}
    # Empty thresholds == today's behaviour: every run passes the gate.
    assert _yml(cfg_dir / "thresholds.yml") == {"thresholds": {}}
    # Nothing is shared until someone opts in.
    assert _yml(cfg_dir / "community.yml") == {
        "display_name": "",
        "community_submit": False,
        "peg_share": False,
        "error_telemetry": False,
    }
    for p, _ in results:  # read on Windows with the system code page
        p.read_bytes().decode("ascii")


def test_ensure_default_configs_never_overwrites(cfg_dir) -> None:
    from stan.setup import ensure_default_configs

    cfg_dir.mkdir(parents=True)
    (cfg_dir / "community.yml").write_text("display_name: Mine\nauth_token: t\n")
    ensure_default_configs()
    results = ensure_default_configs()
    assert all(not created for _, created in results)
    assert (cfg_dir / "community.yml").read_text() == "display_name: Mine\nauth_token: t\n"


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission bits")
def test_default_community_yml_is_owner_only(cfg_dir) -> None:
    from stan.setup import ensure_default_configs

    ensure_default_configs()
    assert ((cfg_dir / "community.yml").stat().st_mode & 0o077) == 0


def test_ensure_default_configs_respects_windows_legacy_dir(tmp_path, monkeypatch) -> None:
    """A new %USERPROFILE%\\STAN\\instruments.yml would shadow the legacy
    ~/.stan one that resolve_config_path() falls back to, and the watcher
    would lose every instrument."""
    import stan.config as cfg
    import stan.setup as st

    home = tmp_path / "home"
    (home / ".stan").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cfg, "_USER_CONFIG_DIR", home / "STAN")
    monkeypatch.setattr(st, "_is_windows", lambda: True)
    legacy = home / ".stan" / "instruments.yml"
    legacy.write_text("instruments:\n- name: Lumos\n  watch_dir: D:/x\n")

    results = dict((p.name, (p, c)) for p, c in st.ensure_default_configs())
    assert results["instruments.yml"] == (legacy, False)
    assert not (home / "STAN" / "instruments.yml").exists()
    assert results["thresholds.yml"][1] is True  # the others are still created
    assert st.instruments_config_path() == legacy


# ── 8. Instrument blocks shared by setup and add-watch ────────────────


def test_normalize_vendor() -> None:
    from stan.setup import normalize_vendor

    assert normalize_vendor(" Bruker ") == "bruker"
    assert normalize_vendor("THERMO") == "thermo"
    assert normalize_vendor(None) is None
    assert normalize_vendor("") is None
    with pytest.raises(ValueError):
        normalize_vendor("sciex")


def test_detect_vendor(tmp_path) -> None:
    from stan.setup import detect_vendor

    assert detect_vendor(tmp_path / "missing") == (None, 0, 0)
    assert detect_vendor(tmp_path) == (None, 0, 0)
    (tmp_path / "proj" / "HeLa_1.d").mkdir(parents=True)
    assert detect_vendor(tmp_path)[0] == "bruker"
    (tmp_path / "a.raw").write_text("")
    (tmp_path / "b.raw").write_text("")
    vendor, n_d, n_raw = detect_vendor(tmp_path)
    assert (vendor, n_d, n_raw) == ("thermo", 1, 2)  # mixed: majority


@pytest.mark.parametrize("vendor, exts, secs", [
    ("bruker", [".d"], 60),
    ("thermo", [".raw"], 30),
])
def test_watcher_block_has_every_key_the_watcher_filters_on(tmp_path, vendor, exts, secs) -> None:
    from stan.setup import watcher_block

    block = watcher_block("timsTOF HT", vendor, tmp_path)
    assert block["vendor"] == vendor
    assert block["extensions"] == exts
    assert block["stable_secs"] == secs
    assert block["enabled"] is True
    assert block["qc_only"] is True
    assert block["watch_dir"] == str(tmp_path.resolve())
    assert "qc_pattern" not in block


def test_upsert_adds_block_with_default_output_dir(cfg_dir, tmp_path) -> None:
    from stan.setup import upsert_instrument_block, watcher_block

    action, block, path = upsert_instrument_block(watcher_block("timsTOF HT", "bruker", tmp_path))
    assert action == "added"
    assert path == cfg_dir / "instruments.yml"
    assert block["output_dir"] == str(cfg_dir / "qc_output" / "timsTOF_HT")
    assert _yml(path)["instruments"] == [block]


def test_upsert_updates_the_same_folder_instead_of_duplicating(cfg_dir, tmp_path) -> None:
    from stan.setup import upsert_instrument_block, watcher_block

    watch = tmp_path / "raw"
    watch.mkdir()
    cfg_dir.mkdir(parents=True)
    # What an older `stan setup` wrote, plus hand-added keys and a second instrument.
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({
        "hive": {"host": "h"},
        "instruments": [
            {"name": "auto", "watch_dir": str(watch) + "/", "enabled": True,
             "hela_amount_ng": 50.0, "community_submit": True,
             "output_dir": "/data/qc", "diann_path": "/opt/diann"},
            {"name": "Lumos", "watch_dir": "/elsewhere", "enabled": True},
        ],
    }, sort_keys=False))

    new = watcher_block("timsTOF HT", "bruker", watch, qc_pattern="(?i)hela")
    new["community_submit"] = None  # None removes the key
    action, block, path = upsert_instrument_block(new)

    doc = _yml(path)
    assert action == "updated"
    assert doc["hive"] == {"host": "h"}
    assert [b["name"] for b in doc["instruments"]] == ["timsTOF HT", "Lumos"]
    b = doc["instruments"][0]
    assert b["vendor"] == "bruker" and b["extensions"] == [".d"] and b["enabled"] is True
    assert b["qc_pattern"] == "(?i)hela"
    assert b["output_dir"] == "/data/qc"          # kept, not replaced by the default
    assert b["diann_path"] == "/opt/diann"        # keys setup does not ask about survive
    assert b["hela_amount_ng"] == 50.0
    assert "community_submit" not in b

    # A second run changes nothing.
    assert upsert_instrument_block(new)[0] == "unchanged"


def test_upsert_drops_duplicate_blocks_for_one_folder_when_asked(cfg_dir, tmp_path) -> None:
    from stan.setup import upsert_instrument_block, watcher_block

    cfg_dir.mkdir(parents=True)
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [
        {"name": "a", "watch_dir": str(tmp_path)},
        {"name": "b", "watch_dir": str(tmp_path)},
    ]}))
    upsert_instrument_block(watcher_block("a", "thermo", tmp_path), drop_duplicates=True)
    assert [b["name"] for b in _yml(cfg_dir / "instruments.yml")["instruments"]] == ["a"]


def test_upsert_refuses_a_name_another_folder_uses(cfg_dir, tmp_path) -> None:
    """The watcher keys instruments by name: a second 'auto' is never watched."""
    from stan.setup import InstrumentNameClash, upsert_instrument_block, watcher_block

    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    upsert_instrument_block(watcher_block("auto", "bruker", tmp_path / "one"))
    before = (cfg_dir / "instruments.yml").read_text()
    with pytest.raises(InstrumentNameClash):
        upsert_instrument_block(watcher_block("auto", "bruker", tmp_path / "two"))
    assert (cfg_dir / "instruments.yml").read_text() == before


def test_upsert_fill_only_never_changes_a_set_value(cfg_dir, tmp_path) -> None:
    from stan.setup import upsert_instrument_block

    cfg_dir.mkdir(parents=True)
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [
        {"name": "Exploris", "watch_dir": str(tmp_path), "vendor": "thermo",
         "enabled": False, "extensions": []},
    ]}))
    action, block, _ = upsert_instrument_block(
        {"watch_dir": str(tmp_path), "vendor": "bruker", "extensions": [".raw"],
         "stable_secs": 30, "enabled": True},
        fill_only=True,
    )
    assert action == "completed"
    assert block["vendor"] == "thermo"      # set: kept
    assert block["enabled"] is False        # an explicit false is a choice: kept
    assert block["extensions"] == [".raw"]  # empty: filled
    assert block["stable_secs"] == 30


def test_upsert_never_overwrites_an_unparseable_file(cfg_dir, tmp_path) -> None:
    from stan.setup import upsert_instrument_block, watcher_block

    cfg_dir.mkdir(parents=True)
    bad = "instruments: [unclosed\n"
    (cfg_dir / "instruments.yml").write_text(bad)
    with pytest.raises(yaml.YAMLError):
        upsert_instrument_block(watcher_block("x", "thermo", tmp_path))
    assert (cfg_dir / "instruments.yml").read_text() == bad


def _watcher_events(block: dict, created: Path) -> tuple[list[str], dict]:
    """Feed one created path through the real watcher handler."""
    from watchdog.events import DirCreatedEvent, FileCreatedEvent

    from stan.watcher.daemon import _AcquisitionHandler

    events: list[str] = []
    trackers: dict = {}
    handler = _AcquisitionHandler(
        dict(block), trackers, {}, threading.Lock(),
        on_event=lambda cat, path, detail: events.append(cat),
    )
    ev = DirCreatedEvent(str(created)) if created.suffix == ".d" else FileCreatedEvent(str(created))
    handler.on_created(ev)
    return events, trackers


# ── 9. The `stan setup` wizard end to end (scripted, no TTY) ──────────


def _script(monkeypatch, answers: dict) -> list[str]:
    """Answer the wizard's prompts by substring; anything else takes its default."""
    import stan.setup as st

    asked: list[str] = []

    def fake(prompt="", *a, **kw):
        asked.append(str(prompt))
        for key, value in answers.items():
            if key in str(prompt):
                return value
        if "default" in kw:
            return kw["default"]
        raise AssertionError(f"unscripted prompt without a default: {prompt!r}")

    for cls in (st.Prompt, st.Confirm, st.FloatPrompt):
        monkeypatch.setattr(cls, "ask", staticmethod(fake))
    return asked


_DECLINE_EXTRAS = {
    "Select column": "1",
    "Participate?": False,
    "Enable daily email report?": False,
    "Enable error reporting?": False,
    "Run baseline builder?": False,
    "Start STAN now?": False,
}


def test_setup_writes_a_block_the_watcher_uses(cfg_dir, tmp_path, monkeypatch) -> None:
    """The audit's finding: setup's block had no vendor/extensions, so the
    watcher ignored every file while the install looked healthy."""
    from stan.setup import run_setup

    watch = tmp_path / "tims"
    (watch / "HeLa_QC_001.d").mkdir(parents=True)
    asked = _script(monkeypatch, {
        "Watch directory": str(watch),
        "Instrument name": "timsTOF HT",
        **_DECLINE_EXTRAS,
    })
    run_setup()

    assert not any(p.strip().startswith("Vendor") for p in asked)  # detected from the .d
    blocks = _yml(cfg_dir / "instruments.yml")["instruments"]
    assert len(blocks) == 1
    b = blocks[0]
    assert b["name"] == "timsTOF HT"
    assert b["vendor"] == "bruker"
    assert b["extensions"] == [".d"]
    assert b["stable_secs"] == 60
    assert b["enabled"] is True
    assert b["qc_only"] is True
    assert b["output_dir"] == str(cfg_dir / "qc_output" / "timsTOF_HT")
    assert "community_submit" not in b

    # The real watcher handler tracks a new QC acquisition with this block ...
    events, trackers = _watcher_events(b, watch / "HeLa_QC_002.d")
    assert events == ["tracked_qc"] and len(trackers) == 1
    # ... and ignored it with the block the old wizard wrote.
    old = {"name": "auto", "watch_dir": str(watch), "enabled": True, "hela_amount_ng": 50.0}
    events, trackers = _watcher_events(old, watch / "HeLa_QC_002.d")
    assert events == ["ignore_ext_mismatch"] and trackers == {}

    # The community answer went to community.yml, where submit-all reads it.
    comm = _yml(cfg_dir / "community.yml")
    assert comm["community_submit"] is False
    assert comm["error_telemetry"] is False
    assert (cfg_dir / "thresholds.yml").exists()


def test_setup_asks_the_vendor_when_the_folder_cannot_tell(cfg_dir, tmp_path, monkeypatch) -> None:
    from stan.setup import run_setup

    watch = tmp_path / "empty"
    watch.mkdir()
    asked = _script(monkeypatch, {
        "Watch directory": str(watch),
        "Vendor": "thermo",
        "Instrument name": "Exploris 480",
        **_DECLINE_EXTRAS,
    })
    run_setup()

    assert any("Vendor" in p for p in asked)
    b = _yml(cfg_dir / "instruments.yml")["instruments"][0]
    assert (b["vendor"], b["extensions"], b["stable_secs"]) == ("thermo", [".raw"], 30)
    events, trackers = _watcher_events(b, watch / "HeLa_50ng_01.raw")
    assert events == ["tracked_qc"] and len(trackers) == 1


def test_setup_rerun_updates_the_folder_block_not_a_second_one(cfg_dir, tmp_path, monkeypatch) -> None:
    from stan.setup import run_setup

    watch = tmp_path / "tims"
    (watch / "HeLa_QC_001.d").mkdir(parents=True)
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [
        {"name": "auto", "watch_dir": str(watch), "enabled": True,
         "hela_amount_ng": 50.0, "community_submit": True, "fasta_path": "/f.fasta"},
        {"name": "Lumos", "watch_dir": str(tmp_path / "lumos"), "enabled": True},
    ]}))
    asked = _script(monkeypatch, {
        "Watch directory": str(watch),
        "Instrument name": "timsTOF HT",
        "Choice": "3",  # every file is QC in this folder
        **_DECLINE_EXTRAS,
    })
    run_setup()

    assert any("Update this instrument's config?" in p for p in asked)
    blocks = _yml(cfg_dir / "instruments.yml")["instruments"]
    assert [b["name"] for b in blocks] == ["timsTOF HT", "Lumos"]
    b = blocks[0]
    assert b["vendor"] == "bruker" and b["extensions"] == [".d"] and b["qc_only"] is False
    assert b["fasta_path"] == "/f.fasta"
    assert "community_submit" not in b
    assert blocks[1] == {"name": "Lumos", "watch_dir": str(tmp_path / "lumos"), "enabled": True}


def test_setup_declining_to_add_leaves_instruments_yml_alone(cfg_dir, tmp_path, monkeypatch) -> None:
    """The old wizard wrote a file holding only the new block when the user
    declined to add it, deleting every other instrument."""
    from stan.setup import run_setup

    watch = tmp_path / "new"
    watch.mkdir()
    cfg_dir.mkdir(parents=True)
    original = yaml.safe_dump({"instruments": [{"name": "Lumos", "watch_dir": "/lumos"}]})
    (cfg_dir / "instruments.yml").write_text(original)
    _script(monkeypatch, {
        "Watch directory": str(watch),
        "Vendor": "thermo",
        "Add this watch directory to existing config?": False,
        **_DECLINE_EXTRAS,
    })
    run_setup()
    assert (cfg_dir / "instruments.yml").read_text() == original


def test_setup_community_yes_goes_to_community_yml(cfg_dir, tmp_path, monkeypatch) -> None:
    import stan.community.pseudonym as pseudonym
    import stan.setup as st

    watch = tmp_path / "tims"
    (watch / "HeLa_QC_001.d").mkdir(parents=True)
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "community.yml").write_text("peg_share: true\nhive_mirror_dir: /m\n")
    monkeypatch.setattr(pseudonym, "generate_unique_pseudonym", lambda: "Clogged PeakTail")
    monkeypatch.setattr(st, "_verify_name_ownership", lambda *a, **k: "tok-1")
    _script(monkeypatch, {
        **_DECLINE_EXTRAS,
        "Watch directory": str(watch),
        "Participate?": True,
        "Already have a STAN name": False,
        "Enable error reporting?": True,
    })
    st.run_setup()

    comm = _yml(cfg_dir / "community.yml")
    assert comm == {
        "peg_share": True,
        "hive_mirror_dir": "/m",
        "community_submit": True,
        "error_telemetry": True,
        "display_name": "Clogged PeakTail",
        "auth_token": "tok-1",
    }
    assert "community_submit" not in _yml(cfg_dir / "instruments.yml")["instruments"][0]

    # stan submit-all / stan verify read it through load_community().
    from stan.config import load_community
    assert load_community()["community_submit"] is True


def test_setup_name_clash_is_asked_again(cfg_dir, tmp_path, monkeypatch) -> None:
    from stan.setup import run_setup

    watch = tmp_path / "second"
    (watch / "HeLa_1.d").mkdir(parents=True)
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [
        {"name": "timsTOF HT", "watch_dir": str(tmp_path / "first")},
    ]}))
    names = iter(["timsTOF HT", "timsTOF HT B"])
    _script(monkeypatch, {
        "Watch directory": str(watch),
        **_DECLINE_EXTRAS,
    })
    import stan.setup as st
    real = st.Prompt.ask

    def ask(prompt="", *a, **kw):
        if "Instrument name" in str(prompt):
            return next(names)
        return real(prompt, *a, **kw)

    monkeypatch.setattr(st.Prompt, "ask", staticmethod(ask))
    run_setup()
    assert [b["name"] for b in _yml(cfg_dir / "instruments.yml")["instruments"]] == [
        "timsTOF HT", "timsTOF HT B",
    ]


# ── 10. community_submit read from a legacy instruments.yml ───────────


def _legacy_setup(cfg_dir: Path, values: list, community: str) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [
        {"name": f"i{n}", "watch_dir": f"/w{n}", "community_submit": v}
        for n, v in enumerate(values)
    ]}))
    (cfg_dir / "community.yml").write_text(community)


def test_legacy_opt_in_is_noted_not_turned_into_consent(cfg_dir, caplog, monkeypatch) -> None:
    """An old per-instrument yes never took effect, so it must not start sharing."""
    import stan.config as config
    from stan.config import load_community

    monkeypatch.setattr(config, "_LEGACY_SUBMIT_NOTED", False)
    _legacy_setup(cfg_dir, [True, True], "display_name: Nimble Edman\nauth_token: tok\n")
    before = (cfg_dir / "community.yml").read_text()
    with caplog.at_level(logging.WARNING, logger="stan.config"):
        assert "community_submit" not in load_community()
        assert "community_submit" not in load_community()
    notes = [r.message for r in caplog.records if "community_submit" in r.message]
    assert len(notes) == 1 and "does not read it there" in notes[0]
    assert (cfg_dir / "community.yml").read_text() == before


def test_a_community_yml_value_always_wins(cfg_dir) -> None:
    from stan.config import load_community

    _legacy_setup(cfg_dir, [True], "community_submit: false\n")
    assert load_community()["community_submit"] is False
    assert (cfg_dir / "community.yml").read_text() == "community_submit: false\n"


def test_legacy_answers_that_disagree_enable_nothing(cfg_dir, caplog) -> None:
    from stan.config import load_community

    _legacy_setup(cfg_dir, [True, False], "display_name: x\n")
    with caplog.at_level(logging.WARNING, logger="stan.config"):
        assert "community_submit" not in load_community()
    assert any("disagree" in r.message for r in caplog.records)
    assert (cfg_dir / "community.yml").read_text() == "display_name: x\n"


def test_legacy_no_is_not_written_anywhere(cfg_dir) -> None:
    from stan.config import load_community

    _legacy_setup(cfg_dir, [False], "display_name: x\n")
    assert "community_submit" not in load_community()
    assert (cfg_dir / "community.yml").read_text() == "display_name: x\n"


# ── 11. The Windows installer's instruments.yml (BOM, legacy .stan dir) ──

# Byte for byte what install_stan.ps1 step 6 writes on a fresh install:
# Windows PowerShell 5.1's `Out-File -Encoding utf8 -NoNewline` puts a UTF-8
# byte-order mark first. (Generated with pwsh's utf8BOM, which writes the
# same bytes, from the installer's own string construction.)
INSTALLER_YML = (
    b"\xef\xbb\xbf# STAN instrument configuration\n"
    b"# Edit this file to add watch directories and instrument names.\n"
    b"instruments: []\n\n"
    b'diann_binary: "C:/Program Files/DIA-NN/2.3.0/diann.exe"\n'
    b'sage_binary: "C:/Users/qc/STAN/tools/sage/sage.exe"'
)


@pytest.fixture()
def ansi_open(monkeypatch) -> None:
    """Text-mode open() without an encoding uses cp1252, as Python on Windows
    does (the ANSI code page), unless it runs in UTF-8 mode."""
    import builtins
    import locale

    real_open = builtins.open

    def cp1252_open(file, mode="r", buffering=-1, encoding=None, *args, **kwargs):
        if "b" not in mode and encoding in (None, "locale"):
            encoding = "cp1252"
        return real_open(file, mode, buffering, encoding, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", cp1252_open)
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "cp1252")


@pytest.fixture()
def win_home(tmp_path, monkeypatch) -> Path:
    """A fresh Windows install: config dir %USERPROFILE%\\STAN (absent), and
    the installer's legacy %USERPROFILE%\\.stan\\instruments.yml."""
    import platform

    import stan.config as cfg

    home = tmp_path / "home"
    (home / ".stan").mkdir(parents=True)
    (home / ".stan" / "instruments.yml").write_bytes(INSTALLER_YML)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(cfg, "_USER_CONFIG_DIR", home / "STAN")
    monkeypatch.setattr(cfg, "_LEGACY_SUBMIT_NOTED", False)
    return home


@pytest.mark.parametrize("raw", [
    INSTALLER_YML,                                        # Out-File -Encoding utf8 (PS 5.1)
    INSTALLER_YML[3:].decode("utf-8").encode("utf-16"),   # Out-File / `>` (UTF-16 + BOM)
], ids=["utf8-bom", "utf16-bom"])
def test_load_yaml_reads_what_windows_powershell_writes(tmp_path, ansi_open, raw) -> None:
    """cp1252 decoded the mark as 'ï»¿' and PyYAML refused the file, so the
    watcher and dashboard saw no instruments on a fresh Windows install."""
    from stan.config import load_yaml

    p = tmp_path / "instruments.yml"
    p.write_bytes(raw)
    doc = load_yaml(p)
    assert doc["instruments"] == []
    assert doc["diann_binary"] == "C:/Program Files/DIA-NN/2.3.0/diann.exe"


def test_load_yaml_still_reads_an_ansi_file(tmp_path, ansi_open) -> None:
    """A file saved as ANSI (not valid UTF-8) reads as open() read it before."""
    from stan.config import load_yaml

    p = tmp_path / "instruments.yml"
    p.write_bytes("instruments:\n- name: Lumos Salle 2\n  watch_dir: D:/r\u00e9sultats\n".encode("cp1252"))
    assert load_yaml(p)["instruments"][0]["watch_dir"] == "D:/r\u00e9sultats"


def test_setup_on_a_fresh_windows_install_writes_where_stan_looks(win_home, ansi_open, tmp_path,
                                                                  monkeypatch, capsys) -> None:
    """install_stan.ps1 wrote .stan\\instruments.yml with a BOM, then offered
    `stan setup`, which could not read it: it wrote no block at all. When the
    file did parse, the block landed in .stan\\, where update_stan.ps1's
    Bruker check (and the installer's own messages) never look."""
    import re

    import stan.config as cfg
    from stan.setup import run_setup

    watch = tmp_path / "tims"
    (watch / "HeLa_QC_001.d").mkdir(parents=True)
    _script(monkeypatch, {"Watch directory": str(watch), "Instrument name": "timsTOF HT",
                          **_DECLINE_EXTRAS})
    run_setup()
    out = " ".join(capsys.readouterr().out.split())

    target = win_home / "STAN" / "instruments.yml"
    doc = _yml(target)
    # The installer's keys came along, and the block the watcher needs is there.
    assert doc["diann_binary"] == "C:/Program Files/DIA-NN/2.3.0/diann.exe"
    assert doc["sage_binary"] == "C:/Users/qc/STAN/tools/sage/sage.exe"
    (b,) = doc["instruments"]
    assert (b["name"], b["vendor"], b["extensions"], b["enabled"]) == ("timsTOF HT", "bruker", [".d"], True)
    assert b["output_dir"] == str(win_home / "STAN" / "qc_output" / "timsTOF_HT")

    # The watcher resolves and parses that file ...
    assert cfg.resolve_config_path("instruments.yml") == target
    assert cfg.load_yaml(target)["instruments"][0]["vendor"] == "bruker"
    # ... and update_stan.ps1's exact detection finds the Bruker block, so it
    # installs alphatims.
    assert re.search(r"(?im)^\s*vendor\s*:\s*['\"]?bruker['\"]?\s*$", target.read_text(encoding="utf-8"))
    # The installer's file is left as it was, and the user is told.
    assert (win_home / ".stan" / "instruments.yml").read_bytes() == INSTALLER_YML
    assert "Copied the settings in" in out and "restart it" in out


def test_setup_block_to_paste_by_hand_has_an_output_dir(cfg_dir, tmp_path, monkeypatch, capsys) -> None:
    """Without one the watcher writes results relative to its working dir."""
    import stan.setup as st

    watch = tmp_path / "tims"
    (watch / "HeLa_QC_001.d").mkdir(parents=True)
    cfg_dir.mkdir(parents=True)
    bad = "instruments: [unclosed\n"
    (cfg_dir / "instruments.yml").write_text(bad)
    _script(monkeypatch, {"Watch directory": str(watch), "Instrument name": "timsTOF HT",
                          **_DECLINE_EXTRAS})
    st.run_setup()
    out = capsys.readouterr().out
    assert "by hand" in out
    assert f"output_dir: {cfg_dir / 'qc_output' / 'timsTOF_HT'}" in out
    assert (cfg_dir / "instruments.yml").read_text() == bad


def test_setup_says_to_restart_a_running_watcher_only_when_it_must(cfg_dir, tmp_path, monkeypatch,
                                                                   capsys) -> None:
    """The daemon's hot-reload starts watchers for new names only; a running
    one keeps the block it started with, so repairing an enabled block did
    nothing until a restart, and nothing said so."""
    from stan.setup import run_setup

    watch = tmp_path / "tims"
    (watch / "HeLa_QC_001.d").mkdir(parents=True)
    other = tmp_path / "exploris"
    other.mkdir()
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [
        {"name": "timsTOF HT", "watch_dir": str(watch), "enabled": True, "hela_amount_ng": 50.0},
    ]}))
    _script(monkeypatch, {"Watch directory": str(watch), **_DECLINE_EXTRAS})
    run_setup()
    assert "restart it" in " ".join(capsys.readouterr().out.split())

    # A new folder is started by the hot-reload itself: no restart note.
    _script(monkeypatch, {"Watch directory": str(other), "Vendor": "thermo",
                          "Instrument name": "Exploris 480", **_DECLINE_EXTRAS})
    run_setup()
    assert "restart it" not in " ".join(capsys.readouterr().out.split())


def test_upsert_override_applies_over_a_set_value(cfg_dir, tmp_path) -> None:
    from stan.setup import upsert_instrument_block

    cfg_dir.mkdir(parents=True)
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [
        {"name": "auto", "watch_dir": str(tmp_path), "qc_only": True, "qc_pattern": "(?i)hela"},
    ]}))
    action, block, _ = upsert_instrument_block(
        {"watch_dir": str(tmp_path), "name": "timsTOF HT", "vendor": "bruker",
         "qc_only": False, "qc_pattern": None},
        fill_only=True, override=("name", "qc_only", "qc_pattern"),
    )
    assert action == "updated"
    assert block["name"] == "timsTOF HT" and block["qc_only"] is False
    assert "qc_pattern" not in block
    assert block["vendor"] == "bruker"  # still filled, as before


def test_setup_qc_filter_asks_again_for_an_invalid_regex(tmp_path, monkeypatch) -> None:
    """compile_qc_pattern never raises (it falls back to the default), so the
    wizard's check accepted a typo and wrote it; the watcher then quietly
    used the default pattern."""
    import stan.setup as st

    answers = iter(["2", "(?i)(hela", "(?i)(hela|k562)"])
    monkeypatch.setattr(st.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(st.Confirm, "ask", staticmethod(lambda *a, **k: True))
    assert st.prompt_qc_filter(tmp_path, "thermo") == (True, "(?i)(hela|k562)")


# ── 12. The QC-filter preview scan is bounded ─────────────────────────


def test_scan_finds_d_folders_without_descending(tmp_path) -> None:
    from stan.setup import _scan_raw_files

    run = tmp_path / "proj" / "HeLa_01.d"
    (run / "nested.d").mkdir(parents=True)  # inside a run: must not be listed
    (run / "analysis.tdf").write_text("x")
    (tmp_path / "HeLa_02.d").mkdir()
    found, truncated = _scan_raw_files(tmp_path, ".d")
    assert sorted(p.name for p in found) == ["HeLa_01.d", "HeLa_02.d"]
    assert truncated is False


def test_scan_stops_at_the_limit(tmp_path) -> None:
    from stan.setup import _scan_raw_files

    for i in range(30):
        (tmp_path / f"r{i}.raw").write_text("x")
    found, truncated = _scan_raw_files(tmp_path, ".raw", limit=10)
    assert truncated is True and len(found) <= 30


# ── 13. Saving email settings never loses the community token ─────────


def test_email_settings_keep_the_token_in_a_bom_file(cfg_dir) -> None:
    from stan.setup import save_email_settings

    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "community.yml").write_bytes(
        b"\xef\xbb\xbfdisplay_name: Nimble Edman\nauth_token: tok\ncommunity_submit: true\n"
    )
    save_email_settings(enabled=True, to="qc@example.org")
    got = _yml(cfg_dir / "community.yml")
    assert got["auth_token"] == "tok" and got["display_name"] == "Nimble Edman"
    assert got["community_submit"] is True
    assert got["email_reports"]["to"] == "qc@example.org"


def test_email_settings_refuse_an_unparseable_file(cfg_dir) -> None:
    from stan.setup import save_email_settings

    cfg_dir.mkdir(parents=True, exist_ok=True)
    bad = "auth_token: tok\n  broken: [\n"
    (cfg_dir / "community.yml").write_text(bad)
    with pytest.raises(yaml.YAMLError):
        save_email_settings(enabled=True, to="qc@example.org")
    assert (cfg_dir / "community.yml").read_text() == bad
