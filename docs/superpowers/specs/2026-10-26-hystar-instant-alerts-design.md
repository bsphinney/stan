# Instant alerts when the timsTOF queue stops — design

**Date:** 2026-10-26
**Status:** approved by Brett 2026-10-26 ("Lets do it"), including the message
mockup at https://claude.ai/artifact/PSBhFWAAPWgzBuAAVsWFER (private to Brett)
**Instrument:** TIMS-10878 (timsTOF HT + Evosep One), the facility's only timsTOF

---

## 1. Why

On Saturday 2026-10-24 an Evotip was not in the position the sample list
expected. The Evosep aborted the injection and HyStar stopped the queue for
the rest of the weekend.

| when (instrument local time) | what | source |
|---|---|---|
| 02:03:36 | `100 samples per day` procedure starts, vial S1-A5 | Evosep `journal.txt` |
| **02:04:24** | `NotifyMissingTip: No Evotip was present during the analysis and the sample was aborted.` → `ABORTED` | Evosep `execution-log.txt` |
| 03:00:33 | STAN posts "Evotip not seated — injection aborted" to Slack, no @mention | `alert_state` row `evotip:tims10878:20261024T0200`, `cron_evosep_20261024.log` |
| Sun 21:30 | STAN re-posts **Friday's** two failures (S4-E1 Evotip 12:32, S4-E2 pressure 12:41) as if new. Brett read this as the alert. | `cron_evosep_20261025.log` 21:30 tick: `alert_state unavailable in PG; using the file fallback` → both keys `"why": "new"` |
| Sun 22:31 | Nightly Bruker check emails the failure | `cron_bruker_maint_20261025.log` (Saturday's Compass backup never reached Hive, so Sunday's carried it) |

Brett did not learn of it until Sunday. Three things went wrong:

1. **Detection is slow.** The Evosep logs are copied to Hive hourly
   (`copy_evosep_logs.ps1`, `-EveryMinutes 60`) and checked every 30 min
   (`cron_evosep.sh`), so an Evosep failure takes about an hour. Failures
   that only Bruker records (MS software errors, lost connections) are seen
   only through the nightly Compass backup, which takes 8–50 h.
2. **Delivery is silent.** The Slack alert names no one, so it does not push
   to a phone.
3. **Stale alerts get re-sent.** When the PG `alert_state` lookup fails even
   once, `AlertStore.last()` (`stan/notify.py`) switches to
   `/quobyte/proteomics-grp/STAN/evosep/alert_state.json` for the rest of
   the tick. That file holds a different, far smaller memory, so every event
   still inside the lookback (72 h for Bruker) is sent again as "new". It
   logs this at INFO without the exception. It happened 14–23 times a day on
   2026-10-17/18, including at least one re-send (cron_evosep 2026-10-17
   17:00), and once on 2026-10-25. Once alerts wake a phone, this becomes
   being woken at night about two-day-old failures, so it is fixed as part
   of this change (§4.5).

**What Brett asked for, in his words:** "I would like to be notified
instantly on any error that stops the acquisition", "I would like it to
catch bruker MS errors too", and "I also want slack to wake my phone and
tell me when this happens."

## 2. Goals, non-goals, anti-goals

**Goals**
- Within about one minute of HyStar stopping the queue for **any** reason
  (Evosep or MS side), Brett's phone buzzes with a Slack message saying what
  stopped it.
- The watcher's own failure is not silent: if it stops running, Brett is told.

**Non-goals**
- Restarting, pausing or otherwise controlling the queue. The watcher is
  **read-only** on everything Bruker and Evosep own.
- Thermo instruments (Lumos, Exploris). This covers TIMS-10878 only.
- Replacing the existing paths. The hourly Evosep alerts (pressure trends,
  clogs, column wear) and the nightly Bruker email stay as they are.
- Non-fatal HyStar noise. `hystar.txt` is full of `[ERROR] CheckMethodExists
  ... failed` lines that stop nothing; those are not alerts.

**Anti-goals** (failing these makes it a failure even if it "works")
- **Pager spam.** The log holds storms: `Evosep One: fan detection fault`
  appears 1,101 times and `Object reference not set to an instance of an
  object.` 1,336 times. One ping per log line would teach Brett to mute the
  channel, which is the failure we are fixing.
- **A watcher that dies quietly.** Silence must never read as "all fine".
  (Project rule: *a watchdog must not live inside what it watches*.)
- **Anything that can disturb acquisition.** No writes on C:\ or D:\ outside
  the watcher's own state folder, no locks held on HyStar's files, and no CPU
  or disk load an operator would notice.
- **Leaking the Slack credential.** It never appears in a log, an event file,
  the repo or the shared Quobyte folder.

## 3. The source: HyStar's error logbook

Found in Brett's 2026-09-02 copy of the instrument PC's `BDalSystemData`
folder, now at
`/quobyte/proteomics-grp/brett/timstof_logs_maybe_useful/BDalSystemData/HyStar/LogFiles/`.

**`CompassHyStarErrorLogbook.log`** gets one `EmergencyShutdown` line each
time HyStar stops the queue, written when it happens. Shutdown messages in
that copy (log created 2024-01-10, copy taken 2026-09-02):

| HyStar message (abridged) | count |
|---|---|
| `Evosep One: Object reference not set to an instance of an object.` | 1,336 (storms) |
| `Evosep One: fan detection fault` | 1,101 (storm, 2025-10) |
| `Evosep One: No Evotip was present during the analysis…` | 100 |
| `Evosep One: High pressure on pump A and/or B detected.` | 27 |
| `Evosep One: pressure limit exceeded` | 30 |
| `Evosep One: Tip expected but not present!` | 10 |
| `Evosep One: Exception has been thrown by the target of an invocation.` | 14 |
| `Evosep One: Low/High pressure pump(s) did not meet criteria…` | 20 |
| `UCEERR_RENDERTHREADFAILURE` (HyStar itself crashing) | 12 |
| **`Critical Error in Bruker OTOF MS - 'Required Python plugin 'proteoscape' not available…'`** | 6 |
| **`Critical Error in Bruker OTOF MS - 'not connected'`** / `Critical error from Bruker OTOF MS! … 'not connected'` | 3 |
| `Evosep One: mcu reset`, `primary power-rail fault`, `Unable to access autosampler`, `MoveToHome`, `FAILED: Average pump flow`, `Upload Sample Conditions canceled`, method errors | 1–6 each |

So one file covers the Evosep side and the MS side.

**Line format** (verbatim):

```
E:2026/08/31 14:39:51,704 !!! EmergencyShutdown Source=ApplicationException Message='Critical Error in ICF System - 'Evosep One: No Evotip was present during the analysis and the sample was aborted.
Please check if the Evotip position in the autosampler matches the sample list.'' !!!
E:2026/09/02 00:27:10,976 Critical error. Error code #1 from ICF System -> 'Evosep One: pressure limit exceeded'
E:2026/09/02 00:27:11,047 !!! EmergencyShutdown Source=PlugInCriticalError Message='Critical error from ICF System! Error #1 -> 'Evosep One: pressure limit exceeded'' !!!
H:################## BDAL Logbook #################
S:2026/08/31 15:16:09,063 ++++++++ Start of error logging for Bruker Compass HyStar 6.3 (6.3.1.8 (1)) ...
```

- Prefixes: `E:` error, `S:` session start (HyStar restarted), `H:` header.
  Some records are preceded by a `Critical error. Error code #N from ...`
  line for the same event. Alert on the **`EmergencyShutdown`** line only.
- **A message can span lines.** The Evotip message has an embedded newline,
  and the record ends at the closing `'' !!!`. Exception records can carry
  a `BDAL.HyStar...Exception:` stack line after them.
- **Encoding: UTF-16LE with a BOM** (`FF FE`). Reading it as ANSI/UTF-8
  produces NUL-interleaved text. Some lines carry stray bytes (`\x13`, a
  non-breaking space shown as `\xff`).
- **Rotation:** at about 2 MB the file is renamed to
  `CompassHyStarErrorLogbook_YYYY_MM_DD_HH_MM_SS.log` and a fresh `.log` is
  started. The one rotation in the copy is
  `CompassHyStarErrorLogbook_2025_10_07_20_52_07.log`, which the fan-fault
  storm filled. **The live file was 1,884,020 bytes on 2026-09-02, so it has
  probably rotated since then.** Rotation handling will be exercised early.

**The sample name** is not in the error logbook. `CompassHyStarLogbook.log`
(same folder, also UTF-16, rotating about every 4 h at 2 MB) logs the
acquisition phases (`PHASE.INJECTED`, `Upload sample data`, ...). It is the
likely place to find the sample or vial that was running. The format of
that line has **not** been confirmed.

**Path on the PC: not verified.** The copy is a `BDalSystemData` tree.
Bruker's usual location is `C:\BDalSystemData`, but nobody has confirmed it
on TIMS-10878, and `find_hystar_logs.ps1` does not list that root. The
watcher locates the file at install (see §4.1) instead of hard-coding a
path nobody has checked.

## 4. Design

```
TIMS-10878 (instrument PC)                           Slack
  HyStar ─► CompassHyStarErrorLogbook.log              ▲  @Brett  (phone buzzes)
                │ read new bytes, every 1 min           │
                ▼                                       │
  watch_hystar_errors.ps1  (scheduled task) ────────────┘ direct post, if the PC can reach Slack
                │ always: event JSON + heartbeat
                ▼
  Y:\STAN\hystar_events\TIMS-10878\  (= /quobyte/proteomics-grp/STAN/hystar_events/TIMS-10878/)
                │
  Hive cron_hystar_events.sh, every 1 min
     • event not posted by the PC → post it (same format, same @mention)
     • record its key in alert_state → hourly Evosep path stays quiet about it
     • heartbeat older than N min → "HyStar watcher stopped" alert, @mention
```

### 4.1 The watcher — `instrument/scripts/watch_hystar_errors.ps1` (+ `.bat`)

Same family as `copy_evosep_logs.ps1` and `copy_bruker_backup.ps1`, and
held to the same rules (see §5):

- **Installs itself** on first run: double-click the `.bat` and accept UAC.
  It copies itself to a stable location, registers a scheduled task
  repeating **every 1 minute** (Task Scheduler's minimum), and supports
  `-Uninstall`. A scheduled task, not a resident process, so a crash or
  reboot cannot leave a dead watcher behind, and `MultipleInstances
  IgnoreNew` stops a slow tick from stacking.
- **Finds the logbook at install.** It checks the likely roots
  (`C:\BDalSystemData`, `D:\BDalSystemData`, and whatever HyStar's own
  config points at, if that is cheap to read), then remembers the path. If
  it finds nothing it fails loudly with the paths it tried.
- **Reads only what is new.** Its state (path, byte offset, file identity or
  first-line fingerprint, last record timestamp) lives under
  `C:\ProgramData\STAN\hystar_watch\`. Each tick it opens the file for
  shared read (`FileShare.ReadWrite`, so HyStar is never blocked), seeks to
  the offset, decodes UTF-16LE and parses complete records. A partial record
  at the end of the file is left for the next tick.
- **Rotation:** if the file is smaller than the saved offset, or its
  fingerprint changed, first finish the rotated file
  (`CompassHyStarErrorLogbook_*.log`, newest) from the saved offset, then
  start the new `.log` at 0. Last-record-timestamp is the backstop against
  replaying old records.
- **First install does not replay history.** Like `--seed` in
  `bruker_alert.py`, it starts at the current end of the file.
- **Per record:** parse the timestamp (instrument local time), the `Source`,
  and the message. Classify it with a small table: Evotip / pressure / MS
  connection / MS software / HyStar crash / Evosep hardware / other. **Every
  `EmergencyShutdown` alerts, including "other"**, because a shutdown is by
  definition a stopped queue. Classification only sets the wording.
- **Vial (reliable for Evosep-side stops):** read `journal.txt` in the
  newest procedure folder under
  `C:\ProgramData\Evosep\EvosepOne\Procedure logs\<serial>\`, the same
  source `copy_evosep_logs.ps1` mirrors. Its format is verified, for
  example `Procedure.Vialposition:Slot1:5 (S1-A5)`. Use it only if that
  procedure started within a few minutes before the shutdown.
- **Sample name (best effort):** look in `CompassHyStarLogbook.log` for the
  sample in progress near the shutdown time. If that can't be found
  reliably, the alert goes out with the vial only, and Hive can fill in the
  `.d` name from PG `runs` when it records the event. It must never delay or
  block the alert.
- **Storms collapse; separate stops never do (my call).** The dedup key is
  the normalized message (digits and paths collapsed). An occurrence is
  folded into the previous alert only if the **same key last occurred under
  5 min earlier**. Any occurrence after a quieter gap alerts again. This
  matters because Evotip messages carry no vial: a miss at 02:04, a remote
  restart at 02:30 and a second miss at 03:15 must give **two** pages, or
  Saturday happens again. A burst stays one alert: the fan-fault storm
  repeated about every 2 min (20:52:07, 20:54:11, 20:56:14). If a burst is
  still going 4 h later, send one reminder ("still happening: N times since
  02:04"). A *different* message always alerts. No dependence on
  `CompassHyStarLogbook.log`. Check the 5 min gap against the real storm
  fixtures (the `Object reference…` storm's spacing is unmeasured) and
  tighten it if a storm's spacing is wider.
- **HyStar restarting unexpectedly (my call).** An `S:` session-start line
  with no `EmergencyShutdown` in the preceding few minutes alerts as
  "HyStar restarted (no shutdown logged)". It is cheap, comes from the same
  file, and catches a hard crash that never wrote a shutdown. A planned
  restart produces one harmless message.
- **Watching Hive back (my call, only if the PC can reach Slack).** Each tick
  the watcher checks the newest `cron_stan_alerts_*.log` under `Y:\STAN\logs\`
  (it already writes to that share). If that log is older than 45 min
  (the cron runs every 20), it posts directly, once, "STAN's Hive alerting
  has gone quiet", with no @mention, plus a recovery line when it returns.
  This covers the open "nothing off-host watches the watchdog" gap
  (2026-09-28/29 login2 outage). If the PC has no internet, it's a known
  limit (§8a).
- **Share unreachable:** event files that can't be written stay queued in
  the watcher's state folder and are written on a later tick. A direct
  Slack post doesn't wait for the share.
- **Delivery (§4.3).** Post directly to Slack when possible, then always
  write the event file.
- **Event file:** one JSON per alert in
  `Y:\STAN\hystar_events\<COMPUTERNAME>\` (UNC fallback
  `\\128.120.208.42\proteomics-grp\STAN\hystar_events\<COMPUTERNAME>\`, the
  same share the copy scripts use). Fields: host, timestamp (local, ISO, no
  zone, matching the other documents), source, category, message, sample
  (if found), dedup key, repeat count, `posted_by_pc` true/false, watcher
  version. **Never the webhook.** Write to `.tmp`, then rename.
- **Heartbeat:** every tick rewrites `heartbeat.json` in the same folder
  (time, version, offset, logbook path, last error if any), including ticks
  with nothing new. That is what makes a dead watcher visible.
- **Own log:** `C:\ProgramData\STAN\hystar_watch\watch.log` with the version
  banner on every run. Keep it small (rotate or truncate past ~1 MB): this
  runs 1,440 times a day.
- **Cost:** a tick should be a stat, a seek and a few KB read. No directory
  walks of `BDalSystemData` after install.

### 4.2 Hive side — `scripts/cron_hystar_events.sh` + a small Python entry point

- Crontab line `* * * * * flock -n /tmp/stan_hystar_events.lock bash
  /quobyte/proteomics-grp/STAN/cron_hystar_events.sh`. It is invoked with
  **`bash <script>`**, guarded with `set +u` around sourcing the system
  profile, writes `logs/cron_hystar_events_YYYYMMDD.log`, and exports
  `STAN_DB_BACKEND=pg`. These are the same rules every other cron here
  follows.
- It reads new event files (and moves or marks them processed, so a file is
  handled once). For each:
  - `posted_by_pc: false` → post it from Hive with the same text and
    @mention.
  - Either way, record its key in `alert_state` (PG) so the existing paths
    don't post the same failure an hour later.
- **Matching the existing keys.** The hourly Evosep path keys point events as
  `evotip:tims10878:<10-min bucket of procedure start>`, and the Bruker path
  as `<kind>:tims10878:<bucket>` (`stan/reports/instrument_watch.py`). Map
  the watcher's categories onto those kinds (`evotip`, `overpressure`,
  `ms_error`, `connection_lost`, …). The existing keys use the **start** of
  the run: the Evosep procedure start (02:03:36 vs the 02:04:24 shutdown on
  Saturday), and Compass `cst.task.start_date` (`instrument/bruker/extract.sql`).
  An MS error can arrive 15–40 min into a run. So mark every bucket of the
  same kind from the shutdown time back over one run length (the longest
  method in use, about 60 min), not only the shutdown's own bucket. The
  builder sets the exact window.
- **Heartbeat check:** add the watcher to the existing silence checks.
  Either extend `CRON_LOGS` in `instrument_watch.py` with the heartbeat
  file, or add a dedicated check, whichever fits. Policy (my call):
  - **Down:** no heartbeat for 10 min → **one** page with @mention: "No
    heartbeat from the HyStar watcher on TIMS-10878: PC off or rebooted,
    nobody logged in, watcher stopped, or the PC can't reach the share. The
    queue is not being watched." A reboot or logoff also means HyStar
    isn't running, so the queue has stopped either way.
  - **No repeats** while it stays down: a 24 h cool-off, then one
    reminder.
  - **Back:** one recovery line, no @mention.
  - **Planned downtime:** `watch_hystar_errors.bat -Pause 8h` (or a
    `pause_until` file in the event folder, which Hive honours) silences the
    down alert until that time. Use it for service visits, holidays and
    planned shutdowns.
  - **Install reports** whether TIMS-10878 auto-logs on after a reboot
    (Winlogon `AutoAdminLogon`). The task runs as the logged-on user, like
    the copy scripts, because HyStar itself needs a logged-on session.
- Login-node safe: it lists one small folder and posts a few messages. No
  SLURM job is needed.

### 4.3 Waking Brett's phone

- Every queue-stopped alert and every watcher-down alert **@mentions
  Brett** (`<@MEMBER_ID>` in the message text). Incoming webhooks support
  this. A mention notifies on mobile where a plain channel post usually does
  not.
- The member ID comes from Brett (Slack → profile → ⋯ → *Copy member ID*).
  It is not secret. It lives in config (`community.yml` / `~/.stan` on
  Hive, a parameter or config file for the watcher), not hard-coded.
- **Message, legible from a lock screen** (follows `render_alerts`):
  first line `🔴 TIMS-10878 queue STOPPED — No Evotip (S1-A5)`, then the
  sample name, the HyStar message verbatim, the time, and a reminder line
  for Evotip misses ("check the Evotip position matches the sample list").
  **Name the vial and the sample, never only an internal log name.** Brett
  could not match Saturday's alert (`Run: 100-samples-per-day_2026-10-24_02-03-36`)
  to a sample. If the watcher can't find the sample, the Hive side should
  add it when it relays or records the event. The `.d` arrives in PG
  `runs` within minutes, with `run_date` about 1 s after the shutdown.
- **Phone settings Brett must allow** (done once by him, listed in the
  install output and the user guide): Slack mobile notifications on for
  mentions, Slack's notification schedule not silencing nights, and the
  phone's sleep / Do Not Disturb mode letting Slack through.
- **Install sends a test message with the @mention**, so "does my phone
  buzz?" is answered on day one, not during the next failure.

### 4.4 The Slack credential on the instrument PC

The direct path needs the webhook URL on TIMS-10878 (Brett accepted this).

- Asked for once at install (pasted), stored in
  `C:\ProgramData\STAN\hystar_watch\` with an ACL limited to
  SYSTEM/Administrators and whatever account the task runs as.
- Never written to the share, an event file or a log. Scrub anything
  shaped like `https://hooks.slack.com/...` from logged errors, as
  `stan.notify` does.
- The installer does **not** read Hive's existing copy at
  `/quobyte/proteomics-grp/brett/.stan_slack_webhook` (used by
  `cron_evosep.sh`). It's pasted at install. Whether Hive's copy should move
  out of the shared tree is a separate question, not part of this change.
- **No internet on the PC is fine.** If the post fails (no route, proxy,
  DNS), set `posted_by_pc: false` and Hive posts it. The cost is up to about
  1 minute of extra delay. The install test reports which route worked.

### 4.5 Fix the alert-memory fallback (in scope)

The new Hive side records keys in `alert_state`, and its alerts now wake a
phone, so the fallback in §1 item 3 has to be fixed in the same change:

- **A PG blip must not make old events "new".** If the PG lookup fails, the
  tick must not send a point event older than a short window (my call: 1 h).
  The fresh HyStar events this change exists for still go out; two-day-old
  ones wait for a tick where PG answers. Standing conditions (feed stale,
  watcher down) still go out, because those are about now.
- **Keep the file a real mirror.** Every successful PG write also writes the
  file, so the fallback, when used, remembers what was sent.
- **Say why.** Log the PG exception at WARNING, with the exception type,
  scrubbed of credentials.
- **Try PG again per call**, or at least once more before giving up for the
  tick. Today a single failure flips the whole tick to the file.
- **One fallback file, for every cron.** Today `cron_evosep.sh` falls back
  to `/quobyte/proteomics-grp/STAN/evosep/alert_state.json` and
  `cron_stan_alerts.sh` to `~/.stan/alert_state.json`, so neither mirror
  covers the other's sends. Use one file, with a lock.
- **Mirror both ways.** A send recorded only in the file during a PG
  failure is written back to PG once PG answers. Otherwise the next normal
  tick re-sends it, which is the same bug in reverse.
- **Measure age in instrument local time.** `notify._parse` stamps naive
  local times as UTC, which would make a fresh event look 7–8 h old and
  hold it back. Use `_parse_local` (or equivalent) for this age test.
- **Held events are not silent (my call).** While events are being held,
  post one standing notice, no @mention: "STAN's alert memory (PG) is
  unavailable; N older events held". It repeats on the normal 12 h standing
  cool-off. Held events go out if PG returns within their lookback. Beyond
  that they are dropped, and the notice is the record that they existed.
  Bruker events are always 8–50 h old when first seen, so a long PG outage
  will hold all of them. That is the price of not re-sending, and the notice
  is what makes it visible.
- **Test:** a store whose PG raises on one call, with a 48 h-old Bruker
  event the file doesn't know, sends nothing for that event and posts the
  held notice. Replay the 2026-10-25 21:30 case.

### 4.6 One injection, one story (in scope, Brett's request)

Saturday's one failed injection produced three Slack posts under three
different names. Brett could not tell they were the same injection:

| time | post | name it used |
|---|---|---|
| 02:25 | QC summary: `timsTOF HT 10212026_HE50_100-spd-dia_S1-A5_1_25352.d — IPS 0 ⚠ no identifications` | the `.d` |
| 03:00 | `Evosep One (TIMS-10878) — Evotip not seated — injection aborted` | `Run: 100-samples-per-day_2026-10-24_02-03-36` (Evosep log folder) |
| (Sun 22:31) | email | Compass well + file |

His words: "can we make that less confusing somehow. I didn't know they
were from the same injection", and of the QC line, "this part was
confusing … as it was an evotip failure".

- **One identity on every post about an injection:** the vial, the sample
  name and the time, in that order. For example `S1-A5 · 10212026_HE50_100-spd-dia · 02:04`.
  This applies to the new queue-stopped alert (§4.3), the hourly Evosep
  alerts and the Bruker alerts. Internal names (Evosep log folder, run
  counter) go after it, or are left out.
- **The QC summary says why a run is empty.** `send_qc_summary`
  (`stan/alerts.py`, called from `stan/pipeline/hive_process.py:802`)
  checks for a recorded stop on the same vial within a couple of minutes of
  the run's `run_date`. If it finds one, the line becomes `⏹ S1-A5 ·
  10212026_HE50_100-spd-dia — no data: injection aborted (No Evotip), see
  02:04 alert` instead of a red IPS 0 / "no identifications". The stop
  record comes from the queue-stopped event (Hive's record of it, §4.2), so
  it already exists ~20 min before the search finishes. If no stop is
  recorded, the line is unchanged. A genuinely bad run must still look bad.
- **Same injection, posted once.** The hourly Evosep alert for an
  injection the watcher already reported is suppressed by key matching
  (§4.2). The QC line is relabelled, not suppressed, so the run is still
  accounted for.
- Threading follow-ups under the first alert needs a Slack bot token
  (`stan/slack_api.py`). Hive runs webhook-only today (`"threaded": false`
  in every tick), so threading is **not** required. Use it if a token is
  configured.

## 5. Constraints (from the project, not negotiable)

- **PowerShell 5.1** on the instrument PC: follow CLAUDE.md's PS rules (no
  `+` concatenation, no inline `if`, no `Where-Object` pipelines,
  `Join-Path`, `return ,$x` where needed, no PS array to a `string[]`
  overload). Write the `.ps1` whole, not patched line by line.
- **`$ScriptVersion` = `stan/__init__.py`**, printed on every run including
  scheduled ones, and enforced by the test (same as the copy scripts). Bump
  `pyproject.toml` + `stan/__init__.py`.
- **`ReadKey` / pause only inside `Pause-IfInteractive`**, which returns
  early under `-Scheduled`.
- **Tests:** there is no PowerShell on the Mac. Use the portable pwsh
  (`~/stan-handoff-2026-09-29/pwsh/pwsh`). Load the functions out of the
  shipped file through the AST (the `tests/test_instrument_copy_scripts.ps1`
  pattern), never regex over its text. Fixtures are real UTF-16LE excerpts
  of the error logbook (with BOM, a multi-line Evotip record, a storm, a
  `S:` restart, an MS `not connected` record, a rotation pair), committed
  under `tests/fixtures/`.
- **Python side:** pytest for event ingest, key mapping (including the
  bucket-before rule), the posted-by-PC branch, the heartbeat alert and the
  @mention rendering. Ruff clean.
- **Hive:** login-node rules from the `ucd-hpc` skill and CLAUDE.md. The
  canonical copy goes in `scripts/` and the running copy in
  `/quobyte/proteomics-grp/STAN/`, not under `brett/`.
- **Docs in the same change:** README (status/TODO), `docs/user_guide.md`,
  `docs/FEATURES.md`, CLAUDE.md's "Instrument-side copies install
  themselves" and Hive cron table, and `instrument/README.md`.

## 6. Verification before calling it done

1. Unit and fixture tests pass (pwsh and pytest).
2. **Replay:** feed the watcher, on a test machine or under pwsh, a logbook
   built from the real 2026-08-31 14:39 Evotip record. One Slack message
   with an @mention, one event file, a heartbeat, and a second tick sends
   nothing.
3. **Storm:** a fixture with 200 fan-fault lines produces exactly one alert,
   plus one reminder once the 4 h window is simulated.
4. **Rotation:** a rotation pair produces no lost and no duplicated record.
5. **Install on TIMS-10878**, by Brett, by double-click. The test message
   buzzes his phone. The install output says which route worked (direct or
   via Hive).
6. **Hive:** with the PC route disabled, an event file is posted from Hive
   within about 2 min, and its key shows in `alert_state`. The next hourly
   Evosep tick does not re-post it.
7. **Dead watcher:** disable the task, and within about 10 min Brett gets
   the "HyStar watcher stopped" mention.
8. **Saturday replay for §4.6:** with Saturday's stop recorded, the QC
   summary for `10212026_HE50_100-spd-dia_S1-A5_1_25352.d` renders as
   "no data: injection aborted (No Evotip)", and the 03:00 Evosep alert
   is suppressed. A real low-ID run with no stop recorded still renders red.
9. Say which surfaces are deployed (Hive pulled, task installed on the PC,
   crontab line added) and which are not.

## 7. Calls I made (Brett: check these)

- Check once a minute via a scheduled task, not a resident file-watcher
  process.
- Every `EmergencyShutdown` alerts, including ones that don't match a known
  category. An `S:` HyStar restart with no shutdown logged before it also
  alerts.
- Storm rule: repeats of the same message collapse only when under 5 min
  apart. A second stop after a restart always pages. One reminder if a
  burst is still going after 4 h.
- Watcher down for 10 min → one page with @mention, a recovery line, no
  repeats for 24 h, and a `-Pause 8h` switch for planned downtime. **This
  means a PC reboot at night (e.g. Windows Update) wakes Brett once.** It's
  consistent with "any error that stops the acquisition", since a reboot
  stops HyStar, but it's the call Brett is most likely to want changed.
- If the PC can reach Slack, it also watches Hive's alerting and says so
  (no @mention) when Hive goes quiet.
- An aborted injection's QC line is relabelled ("no data: injection
  aborted"), not hidden, so every run still shows up in Slack (§4.6).
- The first install starts at the end of the log (no replay of history).
- Severity: every queue stop is treated as critical, even Evotip misses,
  which the hourly path calls "warning".
- When PG doesn't answer, point events older than 1 h are held back rather
  than risk a re-send, and one un-mentioned notice says how many are held
  (§4.5).

## 8. Left to the builder

- Exact state-file format, file-identity method for rotation, and event-file
  naming.
- Whether the Hive side is a new CLI command (`stan hystar-events`) or a
  script. It must be importable and testable either way.
- Exact category table and message wording, within §4.3.
- How the task's run-as account is set (follow the copy scripts unless that
  fails to see `BDalSystemData`).
- How the member ID reaches the watcher (parameter, config on the PC, or a
  non-secret file on the share).

## 8a. Known limits (written into the user guide)

- **A HyStar hang that writes nothing** (process alive, queue frozen, no log
  line) is not caught. Neither is HyStar closed by hand. Catching them needs
  "logbook silent while a queue was running", which depends on the
  unconfirmed `CompassHyStarLogbook.log` format and would fire when a queue
  simply finishes. Candidate follow-up once that format is confirmed.
- **No internet on the PC *and* Hive down at the same time** means no
  alert. With internet, the PC posts queue stops directly and watches Hive
  back (§4.1). Without it, delivery depends on login2's crontab and
  Quobyte.
- The Thermo instruments are not covered.

## 9. Noticed while investigating, not part of this change

- **False "stopped arriving 52 days ago" critical alert** at 2026-10-23
  23:20 (`cron_stan_alerts_20261023.log`, keys `evosep_stale` /
  `bruker_stale`). The feeds were healthy. The Slack message itself quotes
  "Newest data 2026-09-01 17:24", so that tick read a September copy of the
  Evosep document, probably the fallback when the PG read failed. Same class
  as §4.5: a PG blip falls back to stale data and the alert announces it as
  current. It needs its own fix: false criticals undercut the phone alerts
  this change adds.
- (Moved into scope as §4.6: the hourly Evosep alert naming the Evosep log
  folder instead of the sample, and the QC summary's "IPS 0 / no
  identifications" for an aborted injection. The vial is in the
  procedure's `journal.txt`, `Procedure.Vialposition:Slot1:5 (S1-A5)`.)
- **Saturday's Compass backup never reached Hive.** On 2026-10-24 the
  22:30 run used the 2026-10-23 weekly backup.
- `cron_bruker_maint` logs `no stan python at
  /quobyte/proteomics-grp/brett/stan_venv/bin/python` every night, while
  that venv is the one the other crons use.
- The PG Farm `stan` database warns of a collation version mismatch (2.36
  vs 2.41) on every connection.
