"""Smoke test: verify methods called via `self.x()` actually exist on the class.

v0.2.231 shipped with _merge_placeholder_runs on WatcherDaemon but called
from InstrumentWatcher.__init__ — every watcher startup crashed. This test
catches the family of "method on wrong class" bugs.
"""
def test_instrument_watcher_methods_exist():
    from stan.watcher.daemon import InstrumentWatcher
    expected = (
        "_merge_placeholder_runs",
        "_persist_resolved_name",
        "_resolve_spd",
        "_store_run",
        "_run_peg_and_drift",
    )
    for m in expected:
        assert hasattr(InstrumentWatcher, m), f"InstrumentWatcher missing {m}"

def test_watcher_daemon_methods_exist():
    from stan.watcher.daemon import WatcherDaemon
    expected = (
        "_auto_merge_aliases",
        "run",
        "_signal_handler",
    )
    for m in expected:
        assert hasattr(WatcherDaemon, m), f"WatcherDaemon missing {m}"
