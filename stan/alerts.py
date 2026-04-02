"""Alert hooks — optional notifications for QC failures.

Supports shell command hooks configured in instruments.yml.
Future: email, Slack integration.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from stan.gating.evaluator import GateDecision, GateResult

logger = logging.getLogger(__name__)


def send_alert(
    decision: GateDecision,
    instrument_name: str,
    run_name: str,
    instrument_config: dict,
) -> None:
    """Send alerts based on QC gate result.

    Checks instrument config for alert hooks and executes them.

    instrument_config may contain:
        alert_on_fail: true/false (default true)
        alert_on_warn: true/false (default false)
        alert_command: shell command template with {instrument}, {run}, {result}, {diagnosis}
    """
    alert_on_fail = instrument_config.get("alert_on_fail", True)
    alert_on_warn = instrument_config.get("alert_on_warn", False)

    should_alert = (
        (decision.result == GateResult.FAIL and alert_on_fail)
        or (decision.result == GateResult.WARN and alert_on_warn)
    )

    if not should_alert:
        return

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
