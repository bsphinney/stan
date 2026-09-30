"""The dashboard's PG -> SQLite mirror runs only where there is a PG.

Before this gate ``_mirror_enabled()`` was ``STAN_PG_REFRESH_SECONDS > 0``
and nothing else, so on every install that is not UC Davis -- no PG Farm,
often no psycopg2 -- the loop started anyway. It never pulled a row, logged
``PG refresh skipped: No module named 'psycopg2'`` at start-up and every
five minutes after, and /api/capabilities reported ``mirror_active: true``.
Found by the LIVE Mode B install test; ``--backend sqlite`` did not help,
because that flag only skips the start-up probe.

The case that must NOT regress is the hosted dashboard. Azure
(``ucd.stan-proteomics.org``) is started with plain uvicorn, never through
``stan dashboard``, and runs with ``STAN_DB_BACKEND=pg``; its live
/api/capabilities on 2026-09-29 answered
``{"db_backend": "pg", "pg_direct": true, "mirror_active": true}``. Several
endpoints there still read the mirrored ``runs`` table, so losing the mirror
would freeze them at the last pull with nothing on screen to say so.

A UC Davis Mac that reads SQLite but has a PG Farm credential (the
"mirror-fed" mode ``_PG_BACKED`` exists to describe) keeps its mirror too.

Nothing here opens a network connection: every PG entry point is replaced
by a function that fails the test if called, and the credential lookup is
pointed at a temporary directory so a real token on the machine running the
tests cannot leak in.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import stan.dashboard.server as server
import stan.db_pg as db_pg
import stan.sync.pg_to_sqlite as pg_to_sqlite

AZURE_CAPS = {"db_backend": "pg", "pg_direct": True, "mirror_active": True}


def _forbidden(name: str):
    def _fail(*_a, **_k):
        raise AssertionError(f"{name} was called: a PG connection was attempted")
    return _fail


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A host with no PG at all, and every PG entry point booby-trapped.

    Returns a controller: ``env.pg()`` switches on STAN_DB_BACKEND=pg,
    ``env.token()`` drops a credential file where pg_configured() looks,
    and ``env.loops`` counts refresh loops the start-up handler created.
    """
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    token_file = tmp_path / "pgfarm_token"
    monkeypatch.setenv("STAN_PGFARM_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(server, "PG_REFRESH_SECONDS", 300)
    # Start-up's cached answer must not leak between tests.
    monkeypatch.setattr(server, "_MIRROR_ACTIVE", None, raising=False)

    # Any of these being reached means the gate let PG traffic through.
    monkeypatch.setattr(pg_to_sqlite, "pull_from_pg", _forbidden("pull_from_pg"))
    monkeypatch.setattr(db_pg, "probe_pg", _forbidden("probe_pg"))
    monkeypatch.setattr(db_pg, "_connect", _forbidden("_connect"))
    monkeypatch.setattr(db_pg, "_resolve_pgpassword", _forbidden("_resolve_pgpassword"))
    monkeypatch.setattr(db_pg, "_mint_jwt", _forbidden("_mint_jwt"))

    # Start-up must not create or migrate the real ~/.stan/stan.db.
    monkeypatch.setattr(server, "init_db", lambda *a, **k: None)

    class Env:
        loops = 0

        @staticmethod
        def pg() -> None:
            monkeypatch.setenv("STAN_DB_BACKEND", "pg")

        @staticmethod
        def token() -> None:
            token_file.write_text("a-long-lived-service-account-secret\n")

    async def fake_loop() -> None:
        Env.loops += 1

    monkeypatch.setattr(server, "_pg_refresh_loop", fake_loop)
    return Env


def _startup(caplog) -> None:
    caplog.set_level(logging.DEBUG)

    async def run() -> None:
        await server.startup()
        await asyncio.sleep(0)  # let a created task run, if one was

    asyncio.run(run())


def _caps() -> dict:
    caps = asyncio.run(server.api_capabilities())
    return {k: caps[k] for k in AZURE_CAPS}


# ── Installs without PG: no mirror ───────────────────────────────────


def test_sqlite_install_has_no_mirror(env):
    assert server._mirror_enabled() is False


def test_sqlite_startup_starts_no_loop_and_warns_nothing(env, caplog):
    """The LIVE Mode B symptom: a warning at start-up and every 300 s."""
    _startup(caplog)
    assert env.loops == 0
    assert not [r for r in caplog.records if "PG refresh skipped" in r.getMessage()]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("PG mirror off" in r.getMessage() for r in caplog.records)


def test_sqlite_capabilities_no_longer_claim_a_mirror(env):
    assert _caps() == {"db_backend": "sqlite", "pg_direct": False,
                       "mirror_active": False}


def test_sqlite_refresh_button_is_a_no_op(env):
    got = asyncio.run(server.api_refresh())
    assert got == {"ok": True, "runs": -1, "direct": False}


def test_psycopg2_missing_is_never_reached(env, caplog, monkeypatch):
    """Even with the real puller restored, a PG-less host never calls it --
    which is what used to produce 'No module named psycopg2' every tick."""
    calls = []
    monkeypatch.setattr(server, "_pull_from_pg_once",
                        lambda: calls.append(1) or -1)
    _startup(caplog)
    asyncio.run(server.api_refresh())
    assert calls == []


# ── Installs with PG: mirror kept ────────────────────────────────────


def test_azure_keeps_its_mirror(env, caplog):
    """STAN_DB_BACKEND=pg under plain uvicorn, as on ucd.stan-proteomics.org."""
    env.pg()
    assert server._mirror_enabled() is True
    _startup(caplog)
    assert env.loops == 1
    assert _caps() == AZURE_CAPS
    assert any("still refreshing" in r.getMessage() for r in caplog.records)


def test_azure_mirror_does_not_depend_on_a_token_file(env):
    """Azure's credential is an app setting, not a file on /quobyte; the
    backend flag alone is enough to keep the mirror."""
    env.pg()
    assert not db_pg.pg_configured()
    assert server._mirror_enabled() is True


def test_pg_refresh_button_still_pulls(env, monkeypatch):
    env.pg()
    monkeypatch.setattr(server, "_pull_from_pg_once", lambda: 7)
    got = asyncio.run(server.api_refresh())
    assert got == {"ok": True, "runs": 7, "direct": False}


def test_mirror_fed_sqlite_dashboard_with_a_token_file(env, caplog):
    """A UC Davis Mac reading SQLite, filled from PG: keeps its mirror."""
    env.token()
    assert server._mirror_enabled() is True
    _startup(caplog)
    assert env.loops == 1
    caps = _caps()
    assert caps["db_backend"] == "sqlite" and caps["mirror_active"] is True


def test_mirror_fed_sqlite_dashboard_with_pgpassword(env, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "x")
    assert server._mirror_enabled() is True


def test_blank_pgpassword_is_not_a_credential(env, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "   ")
    assert server._mirror_enabled() is False


def test_backend_value_is_case_insensitive(env, monkeypatch):
    monkeypatch.setenv("STAN_DB_BACKEND", "PG")
    assert server._mirror_enabled() is True


def test_backend_sqlite_is_not_pg(env, monkeypatch):
    monkeypatch.setenv("STAN_DB_BACKEND", "sqlite")
    assert server._mirror_enabled() is False


# ── The off switch still wins everywhere ─────────────────────────────


@pytest.mark.parametrize("seconds", [0, -1])
def test_refresh_seconds_zero_turns_it_off_even_on_pg(env, caplog, monkeypatch,
                                                      seconds):
    env.pg()
    env.token()
    monkeypatch.setattr(server, "PG_REFRESH_SECONDS", seconds)
    assert server._mirror_enabled() is False
    _startup(caplog)
    assert env.loops == 0
    assert any("mirror disabled" in r.getMessage() for r in caplog.records)


# ── An unreadable credential path is "no credential", never a crash ──
#
# pg_configured() asks Path.exists() about each token path, and pathlib
# raises PermissionError on EACCES instead of answering False (Python 3.11
# on Hive, 3.13 on the Mac). The default path sits under
# /quobyte/proteomics-grp, which is ``drwxrws--- brettsp proteomics-grp``,
# so for every Hive user outside that group the gate raised out of
# startup() and uvicorn logged "Application startup failed. Exiting." --
# a dashboard that had started fine when the gate was a bare
# PG_REFRESH_SECONDS check.


@pytest.fixture
def locked_token(env, monkeypatch, tmp_path):
    """Point the credential lookup at a file inside a mode-000 directory."""
    import os
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("POSIX directory permissions")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root is not refused by a mode-000 directory")
    locked = tmp_path / "locked_dir"
    locked.mkdir()
    token_file = locked / ".pgfarm_token"
    token_file.write_text("a-long-lived-service-account-secret\n")
    locked.chmod(0)
    monkeypatch.setenv("STAN_PGFARM_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(server, "_CRED_UNREADABLE_WARNED", False, raising=False)
    try:
        # Only meaningful where the lookup really is refused; on a Python
        # whose Path.exists() swallows EACCES the gate was never at risk.
        try:
            token_file.exists()
        except PermissionError:
            pass
        else:
            pytest.skip("this Python's Path.exists() does not raise on EACCES")
        yield env
    finally:
        locked.chmod(0o700)


def test_unreadable_token_path_means_no_mirror(locked_token):
    assert server._mirror_enabled() is False


def test_unreadable_token_path_startup_completes(locked_token, caplog):
    _startup(caplog)
    assert locked_token.loops == 0
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 1 and "locked_dir" in warned[0].getMessage()


def test_unreadable_token_path_capabilities_and_refresh(locked_token):
    assert _caps() == {"db_backend": "sqlite", "pg_direct": False,
                       "mirror_active": False}
    got = asyncio.run(server.api_refresh())
    assert got == {"ok": True, "runs": -1, "direct": False}


def test_unreadable_token_path_warns_once(locked_token, caplog):
    """/api/capabilities is asked on every page load; one line, not one each."""
    caplog.set_level(logging.DEBUG)
    for _ in range(3):
        server._mirror_enabled()
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


@pytest.mark.parametrize("exc", [PermissionError(13, "Permission denied"),
                                 OSError(5, "Input/output error")])
def test_credential_check_oserror_never_escapes(env, caplog, monkeypatch, exc):
    """Platform-independent: whatever OSError the filesystem answers with
    (EACCES, or EIO from a wedged network mount), the gate says no."""
    def _raise() -> bool:
        raise exc
    monkeypatch.setattr(db_pg, "pg_configured", _raise)
    monkeypatch.setattr(server, "_CRED_UNREADABLE_WARNED", False, raising=False)
    assert server._mirror_enabled() is False
    _startup(caplog)
    assert env.loops == 0
    assert _caps()["mirror_active"] is False


def test_pg_backend_does_not_touch_the_credential_path(env, monkeypatch):
    """Azure short-circuits on STAN_DB_BACKEND=pg before any file check."""
    env.pg()
    monkeypatch.setattr(db_pg, "pg_configured", _forbidden("pg_configured"))
    assert server._mirror_enabled() is True


def test_capabilities_report_what_startup_did(env, caplog, monkeypatch):
    """A credential that appears after start-up (a share mounted later) must
    not make /api/capabilities claim a mirror whose loop never started."""
    monkeypatch.setattr(server, "_mirror_enabled", lambda: False)
    _startup(caplog)
    monkeypatch.setattr(server, "_mirror_enabled", lambda: True)
    assert _caps()["mirror_active"] is False
