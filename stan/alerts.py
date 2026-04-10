"""Alert hooks — notifications for QC failures.

Supports:
- Shell command hooks in instruments.yml (legacy)
- Slack webhooks in community.yml (preferred)

Slack config in ~/STAN/community.yml:

    slack_webhook_url: "https://hooks.slack.com/services/T.../B.../..."
    alerts:
      on_qc_fail: true       # alert when a run fails gates
      on_qc_warn: false      # alert on warnings too
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

from stan.gating.evaluator import GateDecision, GateResult

logger = logging.getLogger(__name__)


def _get_slack_webhook() -> str | None:
    """Load Slack webhook URL from community.yml."""
    try:
        from stan.config import load_community
        comm = load_community()
        url = comm.get("slack_webhook_url", "")
        if url and url.startswith("https://hooks.slack.com"):
            return url
    except Exception:
        pass
    return None


def _post_to_slack_async(webhook: str, payload: dict) -> None:
    """Fire-and-forget POST to Slack webhook."""
    def _send():
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                webhook,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            logger.debug("Slack webhook failed", exc_info=True)
    thread = threading.Thread(target=_send, daemon=True, name="stan-slack-alert")
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

    emoji = {":x:": "FAIL", ":warning:": "WARN"}.get(decision.result.value, "")
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
    """Execute a shell alert command with template substitution."""
    cmd = (
        command_template
        .replace("{instrument}", instrument)
        .replace("{run}", run_name)
        .replace("{result}", decision.result.value)
        .replace("{diagnosis}", decision.diagnosis)
        .replace("{failed_gates}", ", ".join(decision.failed_gates))
    )

    try:
        subprocess.run(
            cmd,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.info("Alert command executed for %s: %s", run_name, decision.result.value)
    except subprocess.TimeoutExpired:
        logger.warning("Alert command timed out for %s", run_name)
    except Exception:
        logger.exception("Alert command failed for %s", run_name)
