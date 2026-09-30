"""The Resend key comes only from the lab's own config or environment.

A live key was hardcoded in stan/reports/daily_email.py (public repo) from
April to September 2026; it must never come back as a fallback.
"""

from __future__ import annotations

import pytest

from stan.reports import daily_email


def test_no_builtin_key_in_the_module():
    import inspect
    import re

    src = inspect.getsource(daily_email)
    assert not re.search(r"re_[A-Za-z0-9_]{16,}", src)
    assert not hasattr(daily_email, "_HARDCODED_RESEND_KEY")


def test_key_from_community_yml(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(daily_email, "load_community", lambda: {"resend_api_key": " k1 "})
    assert daily_email._get_resend_api_key() == "k1"


def test_key_from_environment(monkeypatch):
    monkeypatch.setattr(daily_email, "load_community", lambda: {})
    monkeypatch.setenv("RESEND_API_KEY", "k2")
    assert daily_email._get_resend_api_key() == "k2"


def test_no_key_means_no_send(monkeypatch):
    def _missing():
        raise FileNotFoundError("community.yml")

    monkeypatch.setattr(daily_email, "load_community", _missing)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert daily_email._get_resend_api_key() == ""

    def _no_network(*a, **k):
        raise AssertionError("must not reach the network without a key")

    monkeypatch.setattr(daily_email.urllib.request, "urlopen", _no_network)
    with pytest.raises(RuntimeError, match="No Resend API key"):
        daily_email._send_email("qc@example.org", "s", "<p>x</p>")
