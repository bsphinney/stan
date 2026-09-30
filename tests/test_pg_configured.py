"""pg_configured() with unreadable credential candidates."""

from __future__ import annotations

import pytest

from stan import db_pg


class _Path:
    def __init__(self, exists: bool | None) -> None:
        self._exists = exists

    def exists(self) -> bool:
        if self._exists is None:
            raise PermissionError(13, "Permission denied")
        return self._exists


@pytest.fixture(autouse=True)
def _no_env_password(monkeypatch):
    monkeypatch.delenv("PGPASSWORD", raising=False)


def test_readable_candidate_after_an_unreadable_one(monkeypatch):
    monkeypatch.setattr(db_pg, "_token_candidates", lambda: [_Path(None), _Path(True)])
    assert db_pg.pg_configured() is True


def test_only_unreadable_candidates_raise(monkeypatch):
    monkeypatch.setattr(db_pg, "_token_candidates", lambda: [_Path(None), _Path(False)])
    with pytest.raises(PermissionError):
        db_pg.pg_configured()


def test_no_candidates_present(monkeypatch):
    monkeypatch.setattr(db_pg, "_token_candidates", lambda: [_Path(False)])
    assert db_pg.pg_configured() is False
