# HyStar Instant Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wake Brett's phone within about a minute of the TIMS-10878 acquisition queue stopping, for any reason, Evosep side or MS side, and tie every later message about the same injection back to that first alert.

**Architecture:**
- **On the instrument PC:** a self-installing PowerShell scheduled task runs once a minute. It tails HyStar's `CompassHyStarErrorLogbook.log` and posts `EmergencyShutdown` records to Slack with an @mention. It always drops an event file and a heartbeat on the Y: share.
- **On Hive:** a once-a-minute cron relays any event the PC couldn't post, and marks each event's keys in `alert_state` so the hourly paths stay quiet about it. It also pages if the heartbeat stops.
- **Python fixes alongside:** the alert-memory fallback that re-sent Friday's failures, a common way of naming an injection across all alerts, and relabelling the QC line for aborted injections.

**Tech Stack:** PowerShell 5.1 (instrument PC; tested with portable pwsh 7.4.6), Python 3.10+ (`stan` package, typer CLI, psycopg2 via `stan.db_pg`), bash cron on Hive login2, Slack Incoming Webhook.

**Spec:** `docs/superpowers/specs/2026-10-26-hystar-instant-alerts-design.md` (approved by Brett 2026-10-26; message mockup https://claude.ai/artifact/PSBhFWAAPWgzBuAAVsWFER)

## Global Constraints

- Work in `~/stan-work` on branch `feat/hystar-instant-alerts`. Not `~/Documents/STAN`: its `.git` has iCloud-offloaded objects and hangs.
- Version **1.2.23** everywhere: `pyproject.toml`, `stan/__init__.py`, `$ScriptVersion` in `copy_evosep_logs.ps1`, `copy_bruker_backup.ps1` and the new `watch_hystar_errors.ps1`. The PS tests enforce equality with `stan/__init__.py`.
- **PowerShell 5.1 rules** (CLAUDE.md):
  - no `+` string concatenation (use `"$a$b"`)
  - no inline `if` expressions
  - no `Where-Object {}` pipelines (use `foreach`)
  - `Join-Path` for paths
  - `return ,$x` only where the caller doesn't wrap in `@()`
  - never pass a PS array to a `string[]` overload
  - write the `.ps1` whole, never patch it line by line
  - `ReadKey`/pause only inside `Pause-IfInteractive`, which returns early under `-Scheduled`
- **PS tests** run with `~/stan-handoff-2026-09-29/pwsh/pwsh -NoProfile -File <test>.ps1`. They load functions from the shipped file through the AST (the `tests/test_instrument_copy_scripts.ps1` pattern), never regex over its text, and never run the script's top level.
- **Python:** `pytest tests/ -k "not integration"` and `ruff check stan/` stay clean. Run `test_config.py` without the DataArchive `TMPDIR` (known HFS+ mtime false failure).
- **Commits:** subject ≤ 50 chars, `type: summary` style, **no AI attribution line** (Brett's rule, enforced by the pre-commit hook; overrides the default attribution).
- **Read-only** on everything Bruker and Evosep own. The watcher opens HyStar's files with `FileShare.ReadWrite` and writes only to `C:\ProgramData\STAN\hystar_watch\` and the share.
- **The Slack webhook URL never appears** in a log, an event file, the repo or the share. Scrub with the `https://hooks\.slack\.com/\S+` pattern before logging (as `stan.notify._scrub`).
- **Hive:** login-node rules (ucd-hpc skill). The cron runs via `bash <script>` under `flock`, with `set +u` around sourcing the system profile and `STAN_DB_BACKEND=pg`. Canonical copy in `scripts/`, running copy in `/quobyte/proteomics-grp/STAN/`. `git pull` in `/quobyte/proteomics-grp/brett/stan` is the Python deploy.
- **Pinned values (spec §4):**
  - **Watcher:** tick every **1 min**. Repeats of one message fold into one alert when **< 5 min** apart, with one reminder after **4 h** of continuous burst. Restart = `S:` line with no shutdown in the prior **5 min**.
  - **Hive-side alerts:** watcher-down after **10 min** of no heartbeat, then a **24 h** cool-off. Hive counts as quiet after **45 min**.
  - **Alert memory:** point events older than **1 h** are held while PG is unavailable. The held notice cools off for **12 h**.
  - **Matching:** vial lookup window **90 min**. Key-suppression window **60 min** back from the shutdown. QC stop-match **±180 s** with a vial, **±90 s** without.
- **Shared names (PS and Python must agree):**
  - **Event categories:** `evotip`, `overpressure`, `connection_lost`, `ms_error`, `hystar_crash`, `evosep_hw`, `other`.
  - **Event types:** `stop`, `restart`, `reminder`, `hive_quiet`, `hive_back`, `test`.
  - **Station:** `tims10878`. **Host:** `TIMS-10878`.
- **Event root on the share:** `Y:\STAN\hystar_events\<COMPUTERNAME>\` (UNC fallback `\\128.120.208.42\proteomics-grp\STAN\hystar_events\<COMPUTERNAME>\`), which is `/quobyte/proteomics-grp/STAN/hystar_events/TIMS-10878/` on Hive.

### Event file contract (`<event_root>/<id>.json`, UTF-8, written `.tmp` → rename)

```json
{"schema": 1, "id": "TIMS-10878_20261024T020424_1", "type": "stop",
 "host": "TIMS-10878", "station": "tims10878",
 "at": "2026-10-24T02:04:24", "source": "ApplicationException",
 "category": "evotip", "title": "No Evotip",
 "message": "Evosep One: No Evotip was present during the analysis and the sample was aborted. Please check if the Evotip position in the autosampler matches the sample list.",
 "vial": "S1-A5", "sample": null, "dedup_key": "evosep one: no evotip was present ...",
 "count": 1, "first_at": "2026-10-24T02:04:24",
 "payload": {"text": "...", "blocks": []},
 "posted_by_pc": true, "posted_at": "2026-10-24T02:05:09", "watcher_version": "1.2.23"}
```

- `heartbeat.json`: `{"at": local ISO, "utc": ISO Z, "version", "logbook", "offset", "last_error"}`.
- `pause_until.json`: `{"until": local ISO, "by", "reason"}`.
- All times are instrument-local naive ISO, matching the other documents, except `heartbeat.utc`.

### Message copy (spec §4.3, Brett-approved mockup)

```
text   : <@{member}> :red_circle: TIMS-10878 queue STOPPED — {title} · {identity}
block 1: <@{member}> :red_circle: *TIMS-10878 queue STOPPED — {title}*\n*{identity}*
block 2: > {message}\n{check line}\n_HyStar stopped the queue at {at:yyyy-MM-dd HH:mm:ss} · posted by the TIMS-10878 watcher_
context: <https://ucd.stan-proteomics.org|STAN dashboard> · Maintenance tab
```

- **`identity`:** `"{vial} · {sample} · {HH:mm}"`, leaving out whichever of vial/sample is missing.
- **Check line, evotip:** `*Check:* the Evotip in {vial}, and that the tray matches the sample list. The queue stays stopped until someone restarts it.`
- **Check line, all other categories:** `The queue stays stopped until someone restarts it.`
- **Titles:** evotip `No Evotip`, overpressure `Pressure limit`, connection_lost `MS not connected`, ms_error `MS software error`, hystar_crash `HyStar crashed`, evosep_hw `Evosep hardware fault`, other `Stopped`.

## Review Focus

1. **HyStar rotates the logbook between two ticks, or while a record is half-written.** Expect no record lost and none sent twice. → Task 8 `rotation pair` test.
2. **Odd bytes in the log:** a stray `\x13`, a NBSP shown as `\xff`, or an offset that lands mid-UTF-16 code unit after a crash. Expect no exception, no desync, and the next record still parses. → Task 6 `odd offset` test.
3. **The share is unreachable for hours, then comes back.** Queued events reach the share once, in order. Events already posted directly are not re-posted by Hive. → Task 8 `share down` test and Task 5 `posted_by_pc` test.
4. **DST fall-back on Sun 2026-11-01 (next week).** Local naive times repeat 01:00–02:00, so age and key arithmetic must neither crash nor hold a fresh event. → Task 2 `ambiguous local time` test.
5. **The webhook is revoked (HTTP 403/404) or the PC has no route.** The event file must carry `posted_by_pc: false` so Hive relays it, and the log must name the HTTP status, never the URL. → Task 8 `webhook refused` test.

---

### Task 1: Instrument time, injection identity, and @mention support

Shared building blocks for every later Python task.

**Files:**
- Create: `stan/instrument_time.py` (move `INSTRUMENT_TZ` + `_parse_local` here from `stan/reports/instrument_watch.py:700-745`, re-export there so existing imports keep working)
- Create: `stan/reports/injection_identity.py`
- Modify: `stan/notify.py` (`Alert` gains `mention: bool = False`; new `slack_mention_id()`; `render_alerts` prefixes the mention)
- Test: `tests/test_injection_identity.py`, `tests/test_notify.py` (append)

**Interfaces:**
- Produces:
  - `stan.instrument_time.INSTRUMENT_TZ`
  - `stan.instrument_time.parse_local(ts: str | datetime | None) -> datetime | None` (naive → `INSTRUMENT_TZ`, aware unchanged)
  - `injection_identity.sample_from_run_name(run_name: str | None) -> tuple[str | None, str | None]` → `(sample, vial)`
  - `injection_identity.identity_line(vial: str | None, sample: str | None, at: datetime | None) -> str`
  - `injection_identity.find_run_near(vial: str | None, at_local: datetime, window_s: int = 180) -> str | None` (PG `runs.run_name`; returns None on any PG error)
  - `notify.slack_mention_id() -> str | None` (order: `$STAN_SLACK_MENTION` → `slack_mention_user` in community.yml → `~/.stan/slack_mention`. Must match `^[UW][A-Z0-9]{6,}$`, else None)
  - `Alert.mention: bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_injection_identity.py
from datetime import datetime
from stan.reports.injection_identity import sample_from_run_name, identity_line

def test_sample_and_vial_come_out_of_a_hystar_name():
    assert sample_from_run_name("10212026_HE50_100-spd-dia_S1-A5_1_25352.d") == \
        ("10212026_HE50_100-spd-dia", "S1-A5")

def test_compass_style_name_too():
    assert sample_from_run_name(
        "23oct26_2jourHeL50-Ker2Anly-newEvoNoodl-tf9d0_100spd_S4-E1_1_25301.d") == \
        ("23oct26_2jourHeL50-Ker2Anly-newEvoNoodl-tf9d0_100spd", "S4-E1")

def test_unrecognised_name_keeps_the_name_and_no_vial():
    assert sample_from_run_name("weird.d") == ("weird", None)
    assert sample_from_run_name(None) == (None, None)

def test_identity_is_vial_sample_time():
    at = datetime(2026, 10, 24, 2, 4, 24)
    assert identity_line("S1-A5", "10212026_HE50_100-spd-dia", at) == \
        "S1-A5 · 10212026_HE50_100-spd-dia · 02:04"
    assert identity_line("S1-A5", None, at) == "S1-A5 · 02:04"
    assert identity_line(None, None, at) == "02:04"
```

```python
# appended to tests/test_notify.py
def test_mention_prefixes_text_and_first_block(monkeypatch, unconfigured):
    monkeypatch.setenv("STAN_SLACK_MENTION", "U0ABC1234")
    a = Alert(key="k", kind="evotip", instrument="TIMS-10878",
              headline="queue STOPPED — No Evotip", severity="critical", mention=True)
    text, blocks = render_alerts([a])
    assert text.startswith("<@U0ABC1234> ")
    assert blocks[0]["text"]["text"].startswith("<@U0ABC1234> ")

def test_no_mention_without_an_id_or_flag(monkeypatch, unconfigured):
    monkeypatch.delenv("STAN_SLACK_MENTION", raising=False)
    a = Alert(key="k", kind="x", instrument="i", headline="h", mention=True)
    assert "<@" not in render_alerts([a])[0]
    monkeypatch.setenv("STAN_SLACK_MENTION", "U0ABC1234")
    b = Alert(key="k", kind="x", instrument="i", headline="h")   # mention False
    assert "<@" not in render_alerts([b])[0]

def test_a_malformed_mention_id_is_ignored(monkeypatch, unconfigured):
    monkeypatch.setenv("STAN_SLACK_MENTION", "@channel")
    assert notify.slack_mention_id() is None
```

- [ ] **Step 2: Run them, expect failures** — `pytest tests/test_injection_identity.py tests/test_notify.py -k "identity or vial or sample or mention" -v` → ImportError / AttributeError.
- [ ] **Step 3: Implement.**
  - `sample_from_run_name` strips the HyStar suffix `_(S\d+-[A-H]\d+)_\d+_\d+\.d$` and returns the vial from group 1. Otherwise it strips a trailing `.d` and returns vial None.
  - `find_run_near`: `SELECT run_name FROM runs WHERE run_date BETWEEN %s AND %s AND run_name LIKE %s ORDER BY abs(extract(epoch from run_date - %s)) LIMIT 1`, with `%_{vial}_%`, or no LIKE when vial is None. Convert `at_local` to UTC first.
  - Mention: the prefix is added only when some alert in the batch has `mention=True` and `slack_mention_id()` returns an id.
- [ ] **Step 4: Run** `pytest tests/test_injection_identity.py tests/test_notify.py tests/test_instrument_watch*.py -v` → all PASS (the `instrument_watch` re-export keeps its tests green).
- [ ] **Step 5: Commit** — `git commit -m "feat: injection identity and Slack mentions"`

---

### Task 2: Alert memory survives a PG blip (spec §4.5)

**Files:**
- Modify: `stan/notify.py`: `AlertStore`, `notify()`
- Test: `tests/test_notify.py`. Replace `test_pg_failure_falls_back_to_the_file`; its `_use_pg is False after one failure` assertion is the bug.

**Interfaces:**
- Consumes: `stan.instrument_time.parse_local`.
- Produces:
  - **`AlertStore` failure handling:**
    - `AlertStore.last_source: str` (`"pg"` or `"file"`, set by each `last()`)
    - `AlertStore.degraded: bool` (any PG failure this instance)
    - `_PG_MAX_CONSECUTIVE_FAILURES = 2`: stop trying PG for the rest of the instance after 2 failures in a row
  - **`AlertStore` file mirror:**
    - every successful PG write also updates the file
    - file rows written while PG failed carry `"pg_pending": true`
    - `AlertStore.sync_pending_to_pg() -> int` (upserts pending rows with `last_sent = greatest(...)`, clears the flag, returns the count)
    - `_file_lock()` (fcntl lock on `<state>.lock`, held around every file read-modify-write)
  - **`notify()` holding:**
    - `notify(..., hold_older_than: timedelta = timedelta(hours=1))`
    - result gains `"held": [keys]`
    - `HELD_NOTICE_KEY = "alert_memory_unavailable"`
  - **Logging:** PG failures log at WARNING with `type(e).__name__` and a scrubbed message, once per instance.

- [ ] **Step 1: Write the failing tests**

```python
def _raising(*a, **k):
    raise RuntimeError("SSL SYSCALL error: EOF detected")

def test_a_pg_blip_does_not_resend_a_two_day_old_event(monkeypatch, tmp_path, posted):
    """Replays 2026-10-25 21:30: PG failed once, Friday's failures went out as new."""
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", _raising)
    old = Alert(key="evotip:tims10878:20261023T1230", kind="evotip",
                instrument="timsTOF HPZ6", headline="Evotip missing / not picked up",
                at="2026-10-23 12:32")
    now = datetime(2026, 10, 26, 4, 30, tzinfo=timezone.utc)   # Sun 21:30 PDT
    res = notify.notify([old], store=store, now=now)
    assert old.key in res["held"]
    texts = [c["body"]["text"] for c in posted]
    assert not any("Evotip missing" in t for t in texts)
    assert any("alert memory (PG) is unavailable" in t for t in texts)

def test_a_fresh_event_still_goes_out_while_pg_is_down(monkeypatch, tmp_path, posted):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", _raising)
    fresh = Alert(key="evotip:tims10878:20261024T0200", kind="evotip",
                  instrument="TIMS-10878", headline="queue STOPPED — No Evotip",
                  at="2026-10-24 02:04")
    now = datetime(2026, 10, 24, 9, 5, tzinfo=timezone.utc)    # 02:05 PDT
    res = notify.notify([fresh], store=store, now=now)
    assert res["sent"] and fresh.key not in res["held"]

def test_pg_is_retried_after_one_failure(monkeypatch, tmp_path):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    calls = {"n": 0}
    def flaky(key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("blip")
        return {"last_sent": "2026-10-23T21:00:29+00:00", "signature": "",
                "kind": "evotip", "thread": None, "ack": None}
    monkeypatch.setattr(store, "_pg_get", flaky)
    assert store.last("a") is None and store.last_source == "file"
    assert store.last("b") is not None and store.last_source == "pg"

def test_sends_during_a_failure_reach_pg_later(monkeypatch, tmp_path):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_put", _raising)
    store.record(_standing(), now=NOW)
    row = json.loads((tmp_path / "s.json").read_text())[_standing().key]
    assert row["pg_pending"] is True
    pushed = []
    monkeypatch.setattr(store, "_pg_put", lambda a, now: pushed.append(a.key))
    assert store.sync_pending_to_pg() == 1 and pushed == [_standing().key]

def test_successful_pg_writes_are_mirrored_to_the_file(monkeypatch, tmp_path):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_put", lambda a, now: None)
    store.record(_standing(), now=NOW)
    assert _standing().key in json.loads((tmp_path / "s.json").read_text())

def test_ambiguous_local_time_on_dst_fallback_is_not_held(monkeypatch, tmp_path, posted):
    """2026-11-01 01:30 happens twice in Los Angeles."""
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", _raising)
    a = Alert(key="evotip:tims10878:20261101T0130", kind="evotip",
              instrument="TIMS-10878", headline="h", at="2026-11-01 01:30")
    now = datetime(2026, 11, 1, 9, 35, tzinfo=timezone.utc)    # second 01:35 PST
    assert a.key not in notify.notify([a], store=store, now=now)["held"]

def test_pg_failure_is_logged_at_warning_without_secrets(monkeypatch, tmp_path, caplog):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", lambda k: (_ for _ in ()).throw(
        RuntimeError(f"bad {HOOK}")))
    store.last("x")
    rec = [r for r in caplog.records if r.levelname == "WARNING"]
    assert rec and "RuntimeError" in rec[0].getMessage() and HOOK not in caplog.text
```

- [ ] **Step 2: Run, expect failures** — `pytest tests/test_notify.py -v` → the new tests FAIL.
- [ ] **Step 3: Implement.**
  - **Hold rule** in `notify()`: an alert with `cool_off_hours is None`, `store.last()` answered from `"file"` with None, and age `now - parse_local(alert.at) > hold_older_than` is held, not sent.
  - **Held notice:** if anything was held, append `Alert(key=HELD_NOTICE_KEY, kind="alert_memory", instrument="STAN", severity="warning", headline=f"STAN's alert memory (PG) is unavailable; {n} older events held", signature="held", cool_off_hours=12.0)`.
  - **DST:** for an ambiguous local time, `parse_local` takes the later offset (`fold=1`), so an event never looks older than it is.
  - **Sync:** call `sync_pending_to_pg()` at the top of `notify()` when `store._use_pg`, inside try/except.
- [ ] **Step 4: Run** `pytest tests/test_notify.py tests/test_instrument_watch*.py -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "fix: PG blip no longer re-sends old alerts"`

---

### Task 3: Hourly alerts name the injection (spec §4.6, first bullet)

**Files:**
- Modify: `instrument/evosep/extract_evosep.py:1083-1095` (flag dict gains `"well": r.get("well")`)
- Modify: `stan/reports/instrument_watch.py`: evotip/aborted alerts (`check_evosep_episodes`, ~line 493) and `check_bruker` (~line 641)
- Test: `tests/test_instrument_watch.py` (append), `instrument/evosep/test_extract_evosep.py` (append)

**Interfaces:**
- Consumes: `identity_line`, `sample_from_run_name`, `find_run_near` (Task 1).
- Produces: every evotip/aborted/Bruker alert's `detail[0]` is the identity line in bold (`*S1-A5 · 10212026_HE50_100-spd-dia · 02:04*`). The Evosep log name moves to a later line, `Evosep log: \`…\``. The Bruker `fname` moves after the quote.

- [ ] **Step 1: Write the failing tests**

```python
def test_evotip_alert_leads_with_vial_sample_time(monkeypatch):
    monkeypatch.setattr("stan.reports.instrument_watch.find_run_near",
                        lambda vial, at, window_s=180: "10212026_HE50_100-spd-dia_S1-A5_1_25352.d")
    doc = {"flags": [{"run": "100-samples-per-day_2026-10-24_02-03-36",
                      "method": "100-samples-per-day", "start": "2026-10-24T02:03:36",
                      "kinds": ["tip"], "severity": "warning", "well": "S1-A5",
                      "tip_pressure_max_bar": 70.1}]}
    now = datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)
    a = [x for x in check_evosep_episodes(doc, now=now) if x.kind == "evotip"][0]
    assert a.detail[0] == "*S1-A5 · 10212026_HE50_100-spd-dia · 02:03*"
    assert any("100-samples-per-day_2026-10-24_02-03-36" in d for d in a.detail[1:])

def test_bruker_alert_leads_with_vial_sample_time():
    doc = {"failures_recent": [{"category": "Evotip missing / not picked up",
           "start_date": "2026-10-23 12:32", "well": "S4-E1",
           "fname": "23oct26_2jourHeL50-Ker2Anly-newEvoNoodl-tf9d0_100spd_S4-E1_1_25301.d",
           "message": "Critical Error in ICF System - 'Evosep One: No Evotip was present'"}]}
    a = check_bruker(doc, now=datetime(2026, 10, 23, 22, 0, tzinfo=timezone.utc))[0]
    assert a.detail[0] == "*S4-E1 · 23oct26_2jourHeL50-Ker2Anly-newEvoNoodl-tf9d0_100spd · 12:32*"
```

and in `test_extract_evosep.py`, `test_tip_flag_carries_the_journal_well`: build a run record with `well="S1-A5"` the way the existing flag tests do. Assert the emitted flag has `well == "S1-A5"`.

- [ ] **Step 2: Run, expect failures** — `pytest tests/test_instrument_watch.py instrument/evosep/test_extract_evosep.py -k "well or leads_with" -v`
- [ ] **Step 3: Implement.** Import `find_run_near` into `instrument_watch` by name so the test can monkeypatch it. Wrap the lookup in try/except so the alert never fails over it. Alert keys do not change: they are what dedup history is built on.
- [ ] **Step 4: Run** `pytest tests/test_instrument_watch*.py instrument/evosep/test_extract_evosep.py -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: hourly alerts name vial and sample"`

---

### Task 4: Hive ingests watcher events

Covers spec §4.2.

**Files:**
- Create: `stan/reports/hystar_events.py`
- Modify: `stan/cli.py`, adding command `hystar-events` next to `instrument-watch` (~line 8213)
- Create: `scripts/cron_hystar_events.sh` (mode 755 in git)
- Modify: `stan/reports/instrument_watch.py` `CRON_LOGS`, adding `"hystar_events": ("cron_hystar_events_20*.log", 0.5)`
- Create: `tests/fixtures/hystar_events/` holding a `stop` event (Saturday), a `posted_by_pc: false` copy, `heartbeat.json`, `pause_until.json`
- Test: `tests/test_hystar_events.py`. `tests/test_cron_scripts_executable.py` picks the new script up by glob; confirm it is listed.

**Interfaces:**
- Consumes:
  - Task 1: `Alert.mention`, `parse_local`
  - Task 2: `AlertStore`, `notify`
  - `stan.notify.post_slack(payload) -> bool`
- Produces:
  - **Paths:** `DEFAULT_EVENT_ROOT = Path("/quobyte/proteomics-grp/STAN/hystar_events")`, `HOST = "TIMS-10878"`
  - **Suppression map:** `SUPPRESS_KINDS: dict[str, tuple[str, ...]] = {"evotip": ("evotip", "aborted"), "overpressure": ("overpressure", "aborted"), "connection_lost": ("connection_lost",), "ms_error": ("ms_error",), "hystar_crash": (), "evosep_hw": ("aborted",), "other": ("aborted",)}`
  - **Event I/O:**
    - `load_new_events(host_dir: Path) -> list[dict]` (top-level `*.json` except `heartbeat.json`/`pause_until.json`, sorted by `at`)
    - `mark_processed(host_dir: Path, event: dict) -> None` (moves to `processed/YYYY-MM/`)
  - **Keys:** `suppression_keys(event: dict, station: str = "tims10878", window_min: int = 60) -> list[str]` (`f"{kind}:{station}:{bucket}"` for each 10-min bucket from `at - window_min` to `at`, using `instrument_watch._bucket` on `_parse(at)` so they equal the hourly path's keys; `[]` for any event type other than `stop`)
  - **Relay:** `relay_event(event: dict, store: AlertStore, now: datetime) -> bool`
    - posts `event["payload"]` plus one context element `Relayed by Hive: the watcher could not reach Slack` only when `posted_by_pc` is false
    - records every suppression key and the event's own key `f"hystar:{station}:{event['id']}"`
  - **Heartbeat:** `check_watcher_heartbeat(host_dir: Path, now: datetime, station: str = "tims10878") -> list[Alert]`
    - down alert: `key="hystar_watcher_down:tims10878"`, `kind="watcher_down"`, critical, `mention=True`, `cool_off_hours=24.0`, headline `No heartbeat from the HyStar watcher`, detail = spec §4.2 text
    - silenced while `pause_until.json`'s `until` is in the future
    - on recovery, after a down alert was sent: `key=f"hystar_watcher_back:tims10878:{down_last_sent}"`, info, no mention, headline `HyStar watcher is reporting again`
    - marker `<host_dir>/.down_alerted` written when down is sent, removed on recovery
  - **QC lookup:** `find_stop_for_run(run_name: str, run_date: str | datetime | None, root: Path = DEFAULT_EVENT_ROOT) -> dict | None`
    - searches top-level and `processed/` for this month and last month
    - matches `type == "stop"` within ±180 s of `run_date` and the same vial, or ±90 s when the event has no vial
  - **Tick:** `run_hystar_tick(root: Path = DEFAULT_EVENT_ROOT, dry_run: bool = False, now: datetime | None = None, store: AlertStore | None = None) -> dict` (summary for the cron log)
  - **CLI:** `stan hystar-events [--root PATH] [--dry-run]` prints the summary JSON

- [ ] **Step 1: Write the failing tests**

```python
def test_suppression_keys_cover_the_hourly_paths_saturday_key():
    ev = _load("stop_saturday.json")          # at 2026-10-24T02:04:24, category evotip
    keys = suppression_keys(ev)
    assert "evotip:tims10878:20261024T0200" in keys      # the 03:00 alert's real key
    assert "aborted:tims10878:20261024T0200" in keys
    assert "evotip:tims10878:20261024T0100" in keys      # mid-run MS errors start earlier

def test_pc_posted_event_is_recorded_not_reposted(tmp_path, posted, store):
    host = _copy_fixture(tmp_path, "stop_saturday.json")  # posted_by_pc: true
    res = run_hystar_tick(tmp_path, store=store, now=SAT_0206)
    assert posted == [] and res["relayed"] == []
    assert store.last("evotip:tims10878:20261024T0200") is not None
    assert not list(host.glob("*.json")) or {p.name for p in host.glob("*.json")} <= {"heartbeat.json"}

def test_unposted_event_is_relayed_once_with_the_relay_note(tmp_path, posted, store):
    _copy_fixture(tmp_path, "stop_saturday_unposted.json")
    run_hystar_tick(tmp_path, store=store, now=SAT_0206)
    run_hystar_tick(tmp_path, store=store, now=SAT_0207)
    assert len(posted) == 1
    assert "Relayed by Hive" in json.dumps(posted[0]["body"])
    assert posted[0]["body"]["text"].startswith("<@")

def test_the_hourly_evosep_tick_is_then_silent(tmp_path, posted, store):
    _copy_fixture(tmp_path, "stop_saturday.json")
    run_hystar_tick(tmp_path, store=store, now=SAT_0206)
    doc = {"flags": [{"run": "100-samples-per-day_2026-10-24_02-03-36", "start": "2026-10-24T02:03:36",
                      "kinds": ["tip"], "severity": "warning", "method": "m"}]}
    from stan.reports.instrument_watch import check_evosep_episodes
    res = notify(check_evosep_episodes(doc, now=SAT_0300), store=store, now=SAT_0300)
    assert res["n_fresh"] == 0

def test_watcher_down_pages_once_then_recovers(tmp_path, posted, store):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 2, 11))
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 2, 40))
    _write_heartbeat(host, "2026-10-24T03:00:00")
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 3, 0))
    bodies = [p["body"]["text"] for p in posted]
    assert len(bodies) == 2 and "<@" in bodies[0] and "reporting again" in bodies[1]

def test_no_heartbeat_ever_means_not_installed_not_down(tmp_path, posted, store):
    (tmp_path / "TIMS-10878").mkdir()
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 3, 0))
    assert posted == []

def test_only_stop_events_suppress_hourly_keys():
    assert suppression_keys({**_load("stop_saturday.json"), "type": "reminder"}) == []

def test_pause_silences_the_down_alert(tmp_path, posted, store):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    (host / "pause_until.json").write_text('{"until": "2026-10-24T10:00:00"}')
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 3, 0))
    assert posted == []

def test_qc_run_finds_its_stop(tmp_path):
    _copy_fixture(tmp_path, "stop_saturday.json", processed=True)
    hit = find_stop_for_run("10212026_HE50_100-spd-dia_S1-A5_1_25352.d",
                            "2026-10-24T09:04:25+00:00", root=tmp_path)
    assert hit and hit["category"] == "evotip"
    assert find_stop_for_run("other_S1-A6_1_25353.d", "2026-10-24T09:04:25+00:00", root=tmp_path) is None
    assert find_stop_for_run("10212026_HE50_100-spd-dia_S1-A5_1_25352.d",
                             "2026-10-24T09:30:00+00:00", root=tmp_path) is None
```

The `store` fixture is a file-only `AlertStore(use_pg=False, path=tmp_path/"s.json")`. `posted` is the `test_notify.py` fixture, moved into `tests/conftest.py` so both files share it, with `STAN_SLACK_MENTION=U0ABC1234` set.

- [ ] **Step 2: Run, expect failures** — `pytest tests/test_hystar_events.py -v` → ImportError.
- [ ] **Step 3: Implement `stan/reports/hystar_events.py` and the CLI command.**
  - Every function is never-raises at the tick level: a bad event file is logged at WARNING and moved to `rejected/`.
  - Relay goes through `post_slack` directly, not `notify()`, so the PC's rendering is posted verbatim. Record the keys only after a successful post.
- [ ] **Step 4: Write `scripts/cron_hystar_events.sh`**, modelled on `scripts/cron_stan_alerts.sh`:
  - **Environment:** `STAN_DB_BACKEND=pg`, `STAN_PGFARM_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token`, `STAN_ALERT_STATE=/quobyte/proteomics-grp/STAN/alert_state.json`, `STAN_SLACK_MENTION` from `/quobyte/proteomics-grp/brett/.stan_slack_mention`, and the webhook export lines from `cron_evosep.sh:180-182`.
  - **Log:** `/quobyte/proteomics-grp/STAN/logs/cron_hystar_events_$(date +%Y%m%d).log`.
  - **Bash fast path (no Python):** if `$ROOT/TIMS-10878` has no `*.json` other than `heartbeat.json`/`pause_until.json`, the heartbeat mtime is under 10 min old, and `.down_alerted` is absent, then append one line `HH:MM:SS idle` and exit 0.
  - **Header comment:** carries the crontab line `* * * * * flock -n /tmp/stan_hystar_events.lock bash /quobyte/proteomics-grp/STAN/cron_hystar_events.sh`.
- [ ] **Step 5: Run** `pytest tests/test_hystar_events.py tests/test_cron_scripts_executable.py tests/test_instrument_watch_freshness.py -v`, then `bash -n scripts/cron_hystar_events.sh` and `git update-index --chmod=+x scripts/cron_hystar_events.sh` → all PASS.
- [ ] **Step 6: Unify the fallback file** (spec §4.5). Set `export STAN_ALERT_STATE=/quobyte/proteomics-grp/STAN/alert_state.json` in `scripts/cron_stan_alerts.sh`, and change `instrument/evosep/cron_evosep.sh:179` to the same path. Rerun `pytest tests/test_cron_scripts_executable.py tests/test_cron_egress_scoping.py -v` → PASS.
- [ ] **Step 7: Commit** — `git commit -m "feat: Hive relays HyStar watcher events"`

---

### Task 5: QC line says why a run is empty (spec §4.6)

**Files:**
- Modify: `stan/alerts.py`
  - `format_qc_summary(..., stop: dict | None = None)`
  - `send_qc_summary(..., run_date: str | datetime | None = None)`
- Modify: `stan/pipeline/hive_process.py:802` to pass `run_date=acq_date`
- Test: `tests/test_qc_summary.py` (append)

**Interfaces:**
- Consumes: `find_stop_for_run` (Task 4), `sample_from_run_name`/`identity_line` (Task 1).
- Produces: when `stop` is given, the line is exactly
  `:black_square_for_stop: *{instrument}* *{vial} · {sample}* — no data: injection aborted ({stop['title']}), see the {posted_at:%H:%M} alert`
  (falls back to `at` when `posted_at` is missing). It has no IPS, no `:warning:` and no red icon.

- [ ] **Step 1: Write the failing tests**

```python
STOP = {"type": "stop", "category": "evotip", "title": "No Evotip", "vial": "S1-A5",
        "at": "2026-10-24T02:04:24", "posted_at": "2026-10-24T02:05:09"}

def test_aborted_injection_is_labelled_not_red():
    line = format_qc_summary("timsTOF HT", "10212026_HE50_100-spd-dia_S1-A5_1_25352.d",
                             {"n_precursors": 0, "ips_score": 0}, None, stop=STOP)
    assert line == (":black_square_for_stop: *timsTOF HT* *S1-A5 · 10212026_HE50_100-spd-dia* "
                    "— no data: injection aborted (No Evotip), see the 02:05 alert")

def test_a_bad_run_without_a_stop_still_looks_bad():
    line = format_qc_summary("timsTOF HT", "x_S1-A6_1_1.d", {"n_precursors": 0, "ips_score": 0}, None)
    assert "no identifications" in line and "injection aborted" not in line

def test_send_looks_the_stop_up(monkeypatch, posted):
    monkeypatch.setattr("stan.alerts.find_stop_for_run", lambda name, when: STOP)
    send_qc_summary("timsTOF HT", "10212026_HE50_100-spd-dia_S1-A5_1_25352.d",
                    {"n_precursors": 0, "ips_score": 0}, None,
                    run_date="2026-10-24T09:04:25+00:00")
    _wait_for_async_post()
    assert "injection aborted" in posted[0]["body"]["text"]
```

(`_wait_for_async_post` joins the `stan-slack-alert` thread. Use the existing pattern in `test_qc_summary.py` if one exists, otherwise `threading.enumerate()` + join.)

- [ ] **Step 2: Run, expect failures** — `pytest tests/test_qc_summary.py -v`
- [ ] **Step 3: Implement.** Import `find_stop_for_run` at module level (so it can be monkeypatched) and call it positionally as `find_stop_for_run(run_name, run_date)`. Wrap the lookup in try/except so a QC run never fails over it.
- [ ] **Step 4: Run** `pytest tests/test_qc_summary.py tests/test_pipeline.py -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: QC line labels aborted injections"`

---

### Task 6: Watcher, part 1: reading and parsing HyStar's error logbook

**Files:**
- Create: `instrument/scripts/watch_hystar_errors.ps1`. This task adds only the parsing functions plus the script's param block and `$ScriptVersion = '1.2.23'`; the top-level body is a stub that returns.
- Create: `tests/fixtures/hystar_logbook/` holding UTF-16LE + BOM files cut from `/quobyte/proteomics-grp/brett/timstof_logs_maybe_useful/BDalSystemData/HyStar/LogFiles/`. Cut them with `iconv`/python, preserving encoding:
  - `evotip_2026-08-31.log`: the 14:39:51 multi-line record plus its neighbours
  - `pressure_2026-09-02.log`: the `Critical error.` line plus its EmergencyShutdown
  - `ms_not_connected.log`
  - `restart.log`: an `H:` + `S:` header with no preceding shutdown
  - `fan_storm.log`: ≥ 200 fan-fault shutdowns about 2 min apart
  - `partial_tail.log`: ends mid-record, without the closing `'' !!!`
- Create: `tests/test_hystar_watcher.ps1` (same harness as `tests/test_instrument_copy_scripts.ps1`: `Check`, `Load-Script`, functions pulled from the AST)

**Interfaces (PowerShell function signatures later tasks call):**
- **`Read-NewLogText -Path <string> -Offset <long>`** → `@{ Text = <string>; NewOffset = <long>; Length = <long> }`
  - opens with `FileShare.ReadWrite` and decodes UTF-16LE
  - offset 0 skips the 2-byte BOM
  - an odd offset is rounded down to even
- **`Split-LogRecords -Text <string> [-PendingSince <datetime>] [-Now <datetime>]`** → `@{ Records = <string[]>; ConsumedChars = <int> }`
  - a record starts at a line beginning `E:`, `S:`, `H:` or `D:`; continuation lines belong to it
  - the last record is held back (not consumed) unless both:
    - the text ends with a newline, and
    - the record is not an `EmergencyShutdown` lacking its closing `!!!`
  - a held record older than 2 min (`PendingSince`) is released anyway
- **`ConvertFrom-LogRecord -Record <string>`** → `$null` or `@{ Type = 'stop'|'restart'; At = <datetime>; Source = <string>; Message = <string> }`
  - `stop` only for `EmergencyShutdown` lines
  - Message strips the `Critical error from ICF System! Error #N -> '` and `Critical Error in ICF System - '` wrappers and the trailing `'' !!!`, keeps embedded newlines as spaces, and keeps the `Bruker OTOF MS - '...'` text intact
- **`Get-StopCategory -Message <string>`** → `@{ Category; Title; Check }` (patterns in this order):
  - `No Evotip|Tip expected but not present` → evotip
  - `pressure limit exceeded|High pressure` → overpressure
  - `OTOF MS.*not connected` → connection_lost
  - `Bruker OTOF MS` → ms_error
  - `UCEERR_|RENDERTHREADFAILURE` → hystar_crash
  - `fan detection|power-rail|mcu reset|Unable to access autosampler|MoveToHome|did not meet criteria|Average pump flow` → evosep_hw
  - anything else → other
  - Titles and check lines: Global Constraints
- **`Get-DedupKey -Message <string>`** → lower-cased, digit runs → `N`, whitespace collapsed

- [ ] **Step 1: Cut the fixtures** (python on the Mac, reading the Quobyte copy via `ssh hive cat`, slicing whole records and re-encoding `utf-16` with BOM). Commit only these small files.
- [ ] **Step 2: Write the failing pwsh tests** in `tests/test_hystar_watcher.ps1`, as `Check` lines:
  - **Evotip fixture:** exactly one `stop` record. Category `evotip`, `At` 2026-08-31 14:39:51, Message starts `Evosep One: No Evotip was present` and contains `matches the sample list`.
  - **Pressure fixture:** one `stop` (the `Critical error.` line is not a stop), category `overpressure`.
  - **MS fixture:** category `connection_lost`, title `MS not connected`.
  - **Restart fixture:** one `restart` record.
  - **Partial tail:** `ConsumedChars` leaves the last record unconsumed. The same text with `-PendingSince` 3 min before `-Now` consumes it.
  - **Odd offset:** `Read-NewLogText` at offset 3 returns text that starts on a char boundary, and `Split-LogRecords` still yields the next whole record.
  - **Stray bytes:** a fixture line containing `\x13` and `\xff` parses without an exception.
  - **Dedup:** `Get-DedupKey` gives the same key for two fan-fault messages that differ only in numbers.
  - **Version:** `$ScriptVersion` equals `stan/__init__.py`'s version.
- [ ] **Step 3: Run, expect failures** — `~/stan-handoff-2026-09-29/pwsh/pwsh -NoProfile -File tests/test_hystar_watcher.ps1` → FAIL lines, non-zero exit.
- [ ] **Step 4: Implement the five functions** in `watch_hystar_errors.ps1`, following the PS 5.1 rules.
- [ ] **Step 5: Run** the same command → all `ok`, exit 0.
- [ ] **Step 6: Commit** — `git commit -m "feat: parse HyStar error logbook"`

---

### Task 7: Watcher, part 2: deciding, identifying, formatting

**Files:**
- Modify: `instrument/scripts/watch_hystar_errors.ps1`
- Create: `tests/fixtures/evosep_procedures/S00230/100-samples-per-day_2026-10-24_02-03-36/journal.txt` (verbatim from the Quobyte mirror) and one older procedure folder
- Test: `tests/test_hystar_watcher.ps1` (append)

**Interfaces:**
- Consumes: Task 6's functions.
- Produces:
  - **`Get-AlertDecision -State <hashtable> -Key <string> -At <datetime>`** → `'new'`, `'fold'` or `'reminder'`, and mutates `$State.keys[$Key]` = `@{ FirstAt; LastAt; Count; ReminderSent }`.
    - `fold` when the key's `LastAt` is < 5 min before `At`
    - `reminder` (once) when folding and `At - FirstAt >= 4 h`
    - otherwise `new`, which resets `FirstAt`/`Count`
  - **`Test-UnexpectedRestart -State <hashtable> -At <datetime>`** → `$true` when `$State.lastShutdownAt` is null or more than 5 min before `At`.
  - **`Get-EvosepVial -ProcedureRoot <string> -At <datetime>`** → `'S1-A5'` or `$null`.
    - takes the newest `<method>_yyyy-MM-dd_HH-mm-ss` folder (name parsed, no disk stat) whose start is ≤ `At` and ≥ `At - 90 min`
    - reads that folder's `journal.txt` with the regex `Procedure\.Vialposition:.*\((S\d+-[A-H]\d+)\)` (same as `JOURNAL_WELL_RE` in `extract_evosep.py:515`)
  - **`Format-StopPayload -Event <hashtable> -MemberId <string> -Host <string>`** → `@{ text; blocks }` exactly per the Message copy section; `-MemberId` empty → no mention prefix.
  - **`Format-ReminderPayload`**, **`Format-RestartPayload`**, **`Format-HiveQuietPayload -Back:<switch>`**. Copy:
    - reminder: `<@id> :red_circle: TIMS-10878 still stopping — {title}: {count} times since {FirstAt:HH:mm}`
    - restart: `<@id> :red_circle: TIMS-10878 HyStar restarted — no shutdown logged · {HH:mm}`
    - hive quiet (no mention): `:large_orange_diamond: STAN's Hive alerting has gone quiet — last check {HH:mm}`
    - hive back (no mention): `:white_check_mark: STAN's Hive alerting is back`

- [ ] **Step 1: Write the failing pwsh tests:**
  - **Burst rule:**
    - fan-storm records fed in order produce exactly 1 `new` and 0 reminders
    - the same storm stretched past 4 h produces exactly 1 `reminder`
    - Evotip at 02:04 then again at 03:15 produces `new` twice (spec §4.1, the restart case)
  - **Restart:** an `S:` 2 min after a shutdown is not unexpected; one 30 min after is.
  - **Vial:** with At = 2026-10-24 02:04:24 → `S1-A5`; with At = 2026-10-24 04:00 (newest folder is more than 90 min old) → `$null`.
  - **Payload:** for the Saturday event with vial `S1-A5`, sample `10212026_HE50_100-spd-dia` and member `U0ABC1234`:
    - `text` is exactly `<@U0ABC1234> :red_circle: TIMS-10878 queue STOPPED — No Evotip · S1-A5 · 10212026_HE50_100-spd-dia · 02:04`
    - block 2 contains `*Check:* the Evotip in S1-A5`
    - block 2 contains `_HyStar stopped the queue at 2026-10-24 02:04:24 · posted by the TIMS-10878 watcher_`
  - **No vial, no sample:** text ends with `— No Evotip · 02:04`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.** Sample name: try `CompassHyStarLogbook.log` only if a line naming the sample can be found within 10 min before `At` by a cheap tail-read (last 256 KB). Otherwise leave it null. This is best effort and must not throw.
- [ ] **Step 4: Run** → all `ok`.
- [ ] **Step 5: Commit** — `git commit -m "feat: watcher decides and formats alerts"`

---

### Task 8: Watcher, part 3: the tick (state, rotation, delivery, heartbeat)

**Files:**
- Modify: `instrument/scripts/watch_hystar_errors.ps1`
- Test: `tests/test_hystar_watcher.ps1` (append). Uses temp dirs for the state folder, the share and the logbook, and a fake sender injected as a scriptblock parameter.

**Interfaces:**
- Consumes: Tasks 6–7.
- Produces:
  - **`Invoke-WatchTick -StateDir <string> -LogbookPath <string> -ShareDir <string> -ProcedureRoot <string> -HiveLogsDir <string> -Now <datetime> -Sender <scriptblock> -MemberId <string>`**: one full tick, never throws. It:
    1. loads/saves `state.json` (offset, length, first-line hash, `lastRecordAt`, `lastShutdownAt`, `keys`, `pendingSince`, `hiveQuiet`)
    2. handles rotation:
       - **detect** rotation as length < offset, or a changed first-200-byte hash
       - **finish the old file:** read the newest `CompassHyStarErrorLogbook_*.log` from the saved offset
       - **restart:** begin the new `.log` at 0
       - **skip** records with `At <= lastRecordAt`
    3. on a first run with no state: seeds offset = current length and sends nothing
    4. for each stop/restart: decide (Task 7), format, then `& $Sender $payload`, which returns `@{ Ok = <bool>; Status = <int|string> }`
    5. writes the event file (contract in Global Constraints) to `ShareDir`; on failure queues it in `StateDir\pending\` and flushes pending in order on later ticks
    6. writes `heartbeat.json` every tick
    7. Hive-quiet: when the newest `cron_stan_alerts_*.log` in `HiveLogsDir` is > 45 min old, sends one hive_quiet; later sends one hive_back; only when a webhook/sender exists
    8. appends to `StateDir\watch.log` with a version banner and rolls it to `watch.log.1` past 1 MB
  - **`Send-SlackPayload -WebhookUrl <string> -Payload <hashtable>`** → `@{ Ok; Status }`
    - `[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12`, `Invoke-WebRequest -Method Post -ContentType 'application/json; charset=utf-8' -TimeoutSec 10 -UseBasicParsing`
    - never throws, and never puts the URL in `Status`

- [ ] **Step 1: Write the failing pwsh tests:**
  - **First run:** with a pre-existing logbook, sends nothing and leaves offset = length.
  - **New Evotip record:** append it to the logbook; tick → sender called once, one event file with `posted_by_pc: true` and `vial` from the procedure fixture, heartbeat written. A second tick sends nothing.
  - **Rotation pair:**
    - write a record, rename the file to `CompassHyStarErrorLogbook_2026_10_24_02_10_00.log`, create a new logbook holding one more record
    - tick → both records delivered exactly once, in order
    - tick again → nothing
  - **Webhook refused:** sender returns `@{Ok=$false; Status=403}`. The event file has `posted_by_pc: false`, and `watch.log` contains `403` but no `hooks.slack.com`.
  - **Share down:** `ShareDir` not writable → event queued in `pending\`. Make it writable → next tick writes it, `pending\` is empty, and the sender is not called again for it.
  - **Partial record:** a record with no closing `!!!` → not sent on tick 1; sent on a tick 3 min later.
  - **Hive quiet:** `HiveLogsDir` newest log 50 min old → one quiet payload across two ticks. A fresh log → one back payload.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** `Invoke-WatchTick` and `Send-SlackPayload`. The script's top level under `-Scheduled` resolves paths/config (Task 9), builds `-Sender` from `Send-SlackPayload` and calls `Invoke-WatchTick`.
- [ ] **Step 4: Run** → all `ok`. Also run `tests/test_instrument_copy_scripts.ps1` → still all `ok`.
- [ ] **Step 5: Commit** — `git commit -m "feat: watcher tick, delivery, heartbeat"`

---

### Task 9: Watcher, part 4: install, secrets, pause, release

**Files:**
- Modify: `instrument/scripts/watch_hystar_errors.ps1`
  - install surface copied in shape from `copy_evosep_logs.ps1`: `Say`, `Pause-IfInteractive`, `Test-Task`, `Test-Elevated`, `Remove-Task`, `Get-InstalledVersion`, `Install-Task`
  - task name `STAN HyStar watcher`, every 1 min, `MultipleInstances IgnoreNew`, `-StartWhenAvailable`, `ExecutionTimeLimit` 5 min, triggers `AtLogOn` + repeat
- Create: `instrument/scripts/watch_hystar_errors.bat`, mirroring `copy_evosep_logs.bat`, with `/uninstall`, `/pause 8h`, `/test`
- Modify: `instrument/scripts/copy_evosep_logs.ps1:112` and `copy_bruker_backup.ps1:68` → `$ScriptVersion = '1.2.23'`
- Modify: `pyproject.toml`, `stan/__init__.py` → `1.2.23`
- Test: `tests/test_hystar_watcher.ps1` (append), `tests/test_instrument_copy_scripts.ps1` (version check covers the watcher)

**Interfaces:**
- Produces:
  - **Parameters:** `-Scheduled`, `-Uninstall`, `-Pause <string>` (`8h`, `2d`), `-Test`, `-SlackMemberId <string>`, `-LogbookPath <string>`
  - **`Find-ErrorLogbook`** → path or `$null`. It tries `-LogbookPath`, then `C:\BDalSystemData\HyStar\LogFiles\CompassHyStarErrorLogbook.log`, then `D:\BDalSystemData\...`. On failure the installer prints every path tried.
  - **`Save-Webhook` / `Get-Webhook`**:
    - `Read-Host -AsSecureString` → `ConvertFrom-SecureString` (DPAPI, CurrentUser) → `StateDir\webhook.dpapi`
    - ACL set to the current user + Administrators
    - must start with `https://hooks.slack.com/` or install refuses
  - **`config.json`** in the state dir: `{logbook, memberId, shareDir, procedureRoot, hiveLogsDir}`, non-secret
  - **`Write-PauseFile -Until <datetime>`** → `pause_until.json` in the share event dir
  - **`Get-AutoLogonStatus`** → `'on'`, `'off'` or `'unknown'` (reads `HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon` `AutoAdminLogon`)
  - **Install test message:** `<@{id}> STAN HyStar watcher v1.2.23 is installed on TIMS-10878. If your phone buzzed, queue-stop alerts will reach you.`
    - if the direct post fails, it writes a `type: "test"` event with `posted_by_pc: false` so Hive relays it, and prints which route worked
    - Task 4's relay posts any `posted_by_pc: false` event regardless of type

- [ ] **Step 1: Write the failing pwsh tests:**
  - **Install surface:** present (the existing test's checks pointed at the new script). `ReadKey` appears only inside `Pause-IfInteractive`.
  - **Task registration:** 1-minute repetition and `IgnoreNew` appear in `Install-Task`'s AST.
  - **Pause:** `-Pause 8h` with a fixed `Now` writes `until` = `Now + 8 h`.
  - **Webhook:** `Save-Webhook` refuses `http://example.com`. No string literal anywhere in the script contains `hooks.slack.com/services`.
  - **Version:** all three scripts' `$ScriptVersion` equal `stan/__init__.py`.
- [ ] **Step 2: Run, expect failures.** Implement. Run both pwsh test files → all `ok`.
- [ ] **Step 3: Run the full Python suite** `pytest tests/ -k "not integration" -q` and `ruff check stan/` → green.
- [ ] **Step 4: Commit** — `git commit -m "feat: HyStar watcher installs itself (v1.2.23)"`

---

### Task 10: Docs, review, merge, deploy, live verification

**Files:**
- Modify:
  - `README.md` (Implementation Status: HyStar instant alerts; TODO: off-host watchdog partly addressed)
  - `docs/user_guide.md`, a new section "Instant queue-stop alerts":
    - install by double-click
    - what the alert looks like (link the mockup)
    - the phone settings Brett must allow: Slack mobile notifications for mentions, Slack's notification schedule, the phone's sleep / Do Not Disturb
    - `/pause 8h`
    - the known limits from spec §8a
  - `docs/FEATURES.md`
  - `CLAUDE.md`:
    - the "Instrument-side copies install themselves" section gains the watcher
    - the Hive cron table gains `cron_hystar_events.sh` (every minute, `bash`)
    - the watchdog section notes the PC watches Hive back
  - `instrument/README.md`, `instrument/scripts/README.md`

- [ ] **Step 1: Write the docs**, then commit: `git commit -m "docs: instant HyStar queue-stop alerts"`.
- [ ] **Step 2: Whole-branch review.** Use superpowers:requesting-code-review against the spec. Fix the findings, then rerun both pwsh test files and `pytest tests/ -k "not integration" -q`.
- [ ] **Step 3: Merge to main** via superpowers:finishing-a-development-branch, then push. (Brett's standing rule: verified work deploys without asking. Stored-data rewrites still need asking; this has none.)
- [ ] **Step 4: Deploy Hive.** All of it is login-node-light:
  - `git pull` in `/quobyte/proteomics-grp/brett/stan`, then check `python -c 'import stan; print(stan.__version__)'` → `1.2.23`
  - `cp -p scripts/cron_hystar_events.sh scripts/cron_stan_alerts.sh instrument/evosep/cron_evosep.sh` to their running locations under `/quobyte/proteomics-grp/STAN/`
  - `mkdir -p /quobyte/proteomics-grp/STAN/hystar_events/TIMS-10878`
  - write Brett's member ID to `/quobyte/proteomics-grp/brett/.stan_slack_mention` (mode 600; **ask Brett for it**: Slack → profile → ⋯ → Copy member ID)
  - add the crontab line from the script header
  - after 2 min, check `cron_hystar_events_$(date +%Y%m%d).log` shows ticks and that the watcher-down page does **not** fire before the PC is installed (Task 4's `no_heartbeat_ever` case).
- [ ] **Step 5: Stage the PC install.** Copy `watch_hystar_errors.{ps1,bat}` to `/quobyte/proteomics-grp/brett/scripts/` (`Y:\brett\scripts\` on the PC). Give Brett the steps:
  1. Double-click the `.bat` and accept UAC.
  2. Paste the webhook and his member ID.
  3. Confirm his phone buzzes.
  4. Read back the route and auto-logon lines it prints.
- [ ] **Step 6: Live checks** (spec §6 items 5–9). Report each as done or not done, with the evidence:
  - install message buzzed (Brett confirms)
  - `heartbeat.json` refreshes every minute on Hive
  - temporarily rename `webhook.dpapi`, run `watch_hystar_errors.bat /test`, and see Hive relay within 2 min with the key in `alert_state`; restore it
  - `/uninstall` (or disable the task) for 11 min → one down page, then re-enable → one back line
  - say which surfaces are live: Hive (version), crontab line, PC task (version printed), and that Azure and the HF Space are unaffected
- [ ] **Step 7: Update memory.** Update `project_watchdog_followups.md` (off-host gap partly closed) and add a project memory for this feature (where things live, what Brett approved, what's left).
