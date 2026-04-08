"""Daily and weekly QC email reports for STAN.

Composes HTML email summaries of per-instrument status and sends via
Resend API. Includes community percentile comparison, IPS trends,
column lifetime, gate failures, and overdue warnings.

Usage:
    stan email-report --send          # send daily report now
    stan email-report --send-weekly   # send weekly summary
    stan email-report --test          # send a test email
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from html import escape
from stan import __version__
from stan.config import get_user_config_dir, load_community, load_instruments

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
FROM_ADDRESS = "STAN QC Reports <noreply@stan-proteomics.org>"
COMMUNITY_API = "https://brettsp-stan.hf.space/api/cohorts"

_HARDCODED_RESEND_KEY = "re_Ld72v6Ru_FnAKT9hYz2XDSP2QPEL16Lr4"


# ── Config helpers ───────────────────────────────────────────────


def _get_resend_api_key() -> str:
    """Resolve Resend API key from community.yml, env var, or hardcoded fallback."""
    comm = load_community()
    key = comm.get("resend_api_key")
    if key:
        return key

    key = os.environ.get("RESEND_API_KEY")
    if key:
        return key

    return _HARDCODED_RESEND_KEY


def get_email_config() -> dict:
    """Load email report config from community.yml.

    Returns:
        Dict with keys: enabled, to, daily, weekly.
        Returns defaults if not configured.
    """
    comm = load_community()
    defaults = {
        "enabled": False,
        "to": "",
        "daily": "07:00",
        "weekly": "monday",
    }
    cfg = comm.get("email_reports", {})
    if not cfg:
        return defaults
    return {
        "enabled": cfg.get("enabled", False),
        "to": cfg.get("to", ""),
        "daily": cfg.get("daily", "07:00"),
        "weekly": cfg.get("weekly", "monday"),
    }


def save_email_config(
    enabled: bool,
    to: str,
    daily: str = "07:00",
    weekly: str = "monday",
) -> None:
    """Write email report config into community.yml under the email_reports key."""
    import yaml

    community_path = get_user_config_dir() / "community.yml"
    community_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        comm = yaml.safe_load(community_path.read_text()) or {}
    except Exception:
        comm = {}

    comm["email_reports"] = {
        "enabled": enabled,
        "to": to,
        "daily": daily,
        "weekly": weekly,
    }

    community_path.write_text(
        yaml.dump(comm, default_flow_style=False, sort_keys=False)
    )
    # Set permissions so only the user can read the token (Unix only)
    if platform.system() != "Windows":
        community_path.chmod(0o600)


# ── Community reference data (cached daily) ──────────────────────

_COHORT_CACHE_PATH = get_user_config_dir() / "cohort_cache.json"
_COHORT_CACHE_MAX_AGE_HOURS = 24


def _fetch_community_cohorts() -> dict:
    """Fetch community cohort stats from the HF Space relay, with daily caching."""
    # Check cache freshness
    if _COHORT_CACHE_PATH.exists():
        try:
            cache = json.loads(_COHORT_CACHE_PATH.read_text())
            cached_at = datetime.fromisoformat(cache.get("_cached_at", "1970-01-01"))
            age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
            if age_hours < _COHORT_CACHE_MAX_AGE_HOURS:
                return cache
        except Exception:
            pass

    # Fetch fresh data
    try:
        req = urllib.request.Request(
            COMMUNITY_API,
            headers={"User-Agent": f"STAN/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        data["_cached_at"] = datetime.now(timezone.utc).isoformat()
        _COHORT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _COHORT_CACHE_PATH.write_text(json.dumps(data, indent=2))
        return data
    except Exception as exc:
        logger.warning("Failed to fetch community cohorts: %s", exc)
        # Return stale cache if available
        if _COHORT_CACHE_PATH.exists():
            try:
                return json.loads(_COHORT_CACHE_PATH.read_text())
            except Exception:
                pass
        return {}


def _compute_percentile(value: float | None, cohort: dict, metric_key: str) -> int | None:
    """Compute approximate percentile rank within a cohort for a given metric.

    Uses the q25/median/q75 from the cohort to estimate position.
    Returns an integer 0-100, or None if data is insufficient.
    """
    if value is None:
        return None

    q25 = cohort.get(f"{metric_key}_q25")
    median = cohort.get(f"{metric_key}_median")
    q75 = cohort.get(f"{metric_key}_q75")

    if median is None:
        return None

    if q25 is not None and value <= q25:
        # Linear interpolation 0-25
        if q25 > 0:
            return max(0, min(25, int(25 * value / q25)))
        return 0
    elif q75 is not None and value >= q75:
        # Estimate 75-100
        spread = q75 - (median or 0)
        if spread > 0:
            extra = (value - q75) / spread
            return min(99, int(75 + 25 * extra / 2))
        return 90
    elif q25 is not None and q75 is not None:
        # Between q25 and q75 (25-75)
        range_val = q75 - q25
        if range_val > 0:
            return int(25 + 50 * (value - q25) / range_val)
        return 50
    else:
        # Only median available
        if value >= median:
            return 60
        return 40


# ── Data gathering ───────────────────────────────────────────────


def _gather_instrument_data(
    period_days: int = 1,
) -> list[dict]:
    """Gather QC data for all configured instruments.

    Args:
        period_days: How many days back to look for runs (1=daily, 7=weekly).

    Returns:
        List of dicts, one per instrument, with all data needed for the email.
    """
    from stan.db import get_runs, get_events, time_since_last_qc, get_column_lifetime

    _, instruments = load_instruments()
    cohorts = _fetch_community_cohorts()

    cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
    results = []

    for inst in instruments:
        name = inst.get("name", "unknown")

        # Time since last QC
        timing = time_since_last_qc(name)

        # Recent runs (within period)
        all_recent = get_runs(instrument=name, limit=500)
        period_runs = [
            r for r in all_recent
            if datetime.fromisoformat(
                r["run_date"].replace("Z", "+00:00")
            ) >= cutoff
        ]

        # Latest run metrics
        latest = all_recent[0] if all_recent else None

        # Column lifetime
        col_life = get_column_lifetime(name)

        # Gate failures in period
        failures = [r for r in period_runs if r.get("gate_result") == "fail"]
        warns = [r for r in period_runs if r.get("gate_result") == "warn"]

        # IPS trend: compare current week avg vs previous week avg
        now = datetime.now(timezone.utc)
        this_week_start = now - timedelta(days=7)
        prev_week_start = now - timedelta(days=14)

        this_week_ips = []
        prev_week_ips = []
        for r in all_recent:
            rd = datetime.fromisoformat(r["run_date"].replace("Z", "+00:00"))
            ips = r.get("ips_score")
            if ips is not None:
                if rd >= this_week_start:
                    this_week_ips.append(ips)
                elif rd >= prev_week_start:
                    prev_week_ips.append(ips)

        ips_current = sum(this_week_ips) / len(this_week_ips) if this_week_ips else None
        ips_previous = sum(prev_week_ips) / len(prev_week_ips) if prev_week_ips else None
        if ips_current is not None and ips_previous is not None:
            diff = ips_current - ips_previous
            if abs(diff) < 2:
                ips_trend = "stable"
            elif diff > 0:
                ips_trend = "up"
            else:
                ips_trend = "down"
        else:
            ips_trend = "unknown"

        # Community percentile for latest run
        precursor_pct = None
        peptide_pct = None
        protein_pct = None
        if latest and cohorts:
            # Try to find matching cohort
            cohort_list = cohorts.get("cohorts", [])
            matching_cohort = None
            for c in cohort_list:
                if c.get("instrument_family", "").lower() in name.lower():
                    matching_cohort = c
                    break
            if not matching_cohort and cohort_list:
                # Use global stats if no instrument match
                matching_cohort = cohorts.get("global", {})

            if matching_cohort:
                precursor_pct = _compute_percentile(
                    latest.get("n_precursors"), matching_cohort, "n_precursors"
                )
                peptide_pct = _compute_percentile(
                    latest.get("n_peptides"), matching_cohort, "n_peptides"
                )
                protein_pct = _compute_percentile(
                    latest.get("n_proteins"), matching_cohort, "n_proteins"
                )

        # Determine status badge
        hours_ago = timing.get("hours_ago")
        has_failures = len(failures) > 0
        if hours_ago is None or hours_ago > 72 or has_failures:
            status = "red"
        elif hours_ago > 24 or len(warns) > 0:
            status = "yellow"
        else:
            status = "green"

        # Events in period
        events = get_events(instrument=name, limit=50)
        period_events = [
            e for e in events
            if datetime.fromisoformat(
                e["event_date"].replace("Z", "+00:00")
            ) >= cutoff
        ]

        results.append({
            "name": name,
            "status": status,
            "timing": timing,
            "latest": latest,
            "period_runs": period_runs,
            "n_runs_period": len(period_runs),
            "n_failures": len(failures),
            "n_warns": len(warns),
            "failures": failures,
            "column_lifetime": col_life,
            "ips_current": round(ips_current, 1) if ips_current else None,
            "ips_trend": ips_trend,
            "precursor_pct": precursor_pct,
            "peptide_pct": peptide_pct,
            "protein_pct": protein_pct,
            "period_events": period_events,
            "depth_trend": col_life.get("depth_trend_pct_per_week"),
        })

    return results


# ── HTML composition ─────────────────────────────────────────────

_STATUS_COLORS = {
    "green": "#22c55e",
    "yellow": "#eab308",
    "red": "#ef4444",
}

_STATUS_LABELS = {
    "green": "OK",
    "yellow": "WATCH",
    "red": "ALERT",
}

_TREND_ARROWS = {
    "up": "&#9650;",      # triangle up
    "down": "&#9660;",    # triangle down
    "stable": "&#8212;",  # em dash
    "unknown": "?",
}


def _fmt_number(val: int | float | None) -> str:
    """Format a number for display, with comma separators."""
    if val is None:
        return "--"
    if isinstance(val, float):
        if val >= 10:
            return f"{int(val):,}"
        return f"{val:.1f}"
    return f"{val:,}"


def _fmt_hours(hours: float | None) -> str:
    """Format hours-ago into a human-readable string."""
    if hours is None:
        return "never"
    if hours < 1:
        return f"{int(hours * 60)} min ago"
    if hours < 24:
        return f"{hours:.1f}h ago"
    days = hours / 24
    return f"{days:.1f} days ago"


def _pct_badge(pct: int | None) -> str:
    """Return an HTML badge for a percentile value."""
    if pct is None:
        return '<span style="color: #6b7280;">--</span>'
    if pct >= 75:
        color = "#22c55e"
    elif pct >= 50:
        color = "#3b82f6"
    elif pct >= 25:
        color = "#eab308"
    else:
        color = "#ef4444"
    return (
        f'<span style="display: inline-block; padding: 2px 8px; border-radius: 4px; '
        f'background: {color}20; color: {color}; font-weight: 600; font-size: 13px;">'
        f'P{pct}</span>'
    )


def _instrument_card_html(inst: dict) -> str:
    """Render a single instrument status card as an HTML block."""
    name = escape(inst["name"])
    status_color = _STATUS_COLORS.get(inst["status"], "#6b7280")
    status_label = _STATUS_LABELS.get(inst["status"], "?")
    timing = inst["timing"]
    latest = inst["latest"]
    col = inst["column_lifetime"]

    # Status badge
    badge = (
        f'<span style="display: inline-block; padding: 4px 12px; border-radius: 6px; '
        f'background: {status_color}20; color: {status_color}; font-weight: 700; '
        f'font-size: 13px; letter-spacing: 0.5px;">{status_label}</span>'
    )

    # Last QC timing
    hours_ago = timing.get("hours_ago")
    timing_str = _fmt_hours(hours_ago)
    overdue_warning = ""
    if hours_ago is not None and hours_ago > 24:
        overdue_warning = (
            ' <span style="color: #ef4444; font-weight: 600;">'
            '&#9888; OVERDUE</span>'
        )

    # Metrics
    precursors = _fmt_number(latest.get("n_precursors")) if latest else "--"
    peptides = _fmt_number(latest.get("n_peptides")) if latest else "--"
    proteins = _fmt_number(latest.get("n_proteins")) if latest else "--"

    # IPS
    ips_val = _fmt_number(inst.get("ips_current"))
    ips_arrow = _TREND_ARROWS.get(inst["ips_trend"], "?")
    ips_color = {"up": "#22c55e", "down": "#ef4444", "stable": "#6b7280"}.get(
        inst["ips_trend"], "#6b7280"
    )

    # Column lifetime
    days_on_col = col.get("days_on_column", 0)
    total_inj = col.get("total_injections_on_column")
    col_info = f"{days_on_col} days"
    if total_inj is not None:
        col_info = f"{total_inj:,} injections ({days_on_col} days)"
    col_model = col.get("column_model") or ""

    # Depth trend
    depth = inst.get("depth_trend")
    if depth is not None:
        depth_color = "#22c55e" if depth >= 0 else "#ef4444"
        depth_str = f'<span style="color: {depth_color};">{depth:+.1f}%/week</span>'
    else:
        depth_str = '<span style="color: #6b7280;">--</span>'

    # Gate failures
    failures_html = ""
    if inst["n_failures"] > 0:
        failures_html = (
            f'<div style="margin-top: 8px; padding: 8px 12px; background: #ef444420; '
            f'border-radius: 6px; border-left: 3px solid #ef4444;">'
            f'<span style="color: #ef4444; font-weight: 600;">'
            f'{inst["n_failures"]} gate failure(s)</span>'
        )
        for f in inst["failures"][:3]:
            failed_gates = json.loads(f.get("failed_gates", "[]")) if isinstance(f.get("failed_gates"), str) else f.get("failed_gates", [])
            gates_str = ", ".join(failed_gates[:3]) if failed_gates else "unknown"
            diag = escape(f.get("diagnosis", "")[:100])
            failures_html += (
                f'<div style="color: #d1d5db; font-size: 12px; margin-top: 4px;">'
                f'{escape(f.get("run_name", ""))} -- {gates_str}'
                f'{f" ({diag})" if diag else ""}</div>'
            )
        failures_html += "</div>"

    return f"""
    <div style="background: #1e293b; border-radius: 10px; padding: 20px; margin-bottom: 16px;
                border: 1px solid #334155;">
      <div style="display: flex; justify-content: space-between; align-items: center;
                  margin-bottom: 12px;">
        <h3 style="margin: 0; font-size: 18px; color: #f1f5f9;">{name}</h3>
        {badge}
      </div>

      <div style="color: #94a3b8; font-size: 13px; margin-bottom: 12px;">
        Last QC: <span style="color: #e2e8f0;">{timing_str}</span>{overdue_warning}
        {f' &mdash; {escape(timing.get("last_run_name", ""))}' if timing.get("last_run_name") else ""}
      </div>

      <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
        <tr>
          <td style="padding: 6px 0; color: #94a3b8;">Precursors</td>
          <td style="padding: 6px 8px; color: #e2e8f0; text-align: right; font-weight: 600;">{precursors}</td>
          <td style="padding: 6px 0; text-align: right;">{_pct_badge(inst.get("precursor_pct"))}</td>
        </tr>
        <tr>
          <td style="padding: 6px 0; color: #94a3b8;">Peptides</td>
          <td style="padding: 6px 8px; color: #e2e8f0; text-align: right; font-weight: 600;">{peptides}</td>
          <td style="padding: 6px 0; text-align: right;">{_pct_badge(inst.get("peptide_pct"))}</td>
        </tr>
        <tr>
          <td style="padding: 6px 0; color: #94a3b8;">Proteins</td>
          <td style="padding: 6px 8px; color: #e2e8f0; text-align: right; font-weight: 600;">{proteins}</td>
          <td style="padding: 6px 0; text-align: right;">{_pct_badge(inst.get("protein_pct"))}</td>
        </tr>
        <tr>
          <td style="padding: 6px 0; color: #94a3b8;">IPS Score</td>
          <td style="padding: 6px 8px; color: #e2e8f0; text-align: right; font-weight: 600;">{ips_val}</td>
          <td style="padding: 6px 0; text-align: right;">
            <span style="color: {ips_color}; font-size: 14px;">{ips_arrow}</span>
          </td>
        </tr>
        <tr>
          <td style="padding: 6px 0; color: #94a3b8;">Column</td>
          <td colspan="2" style="padding: 6px 0; color: #e2e8f0; text-align: right; font-size: 12px;">
            {escape(col_model)} &mdash; {col_info}
          </td>
        </tr>
        <tr>
          <td style="padding: 6px 0; color: #94a3b8;">Depth Trend</td>
          <td colspan="2" style="padding: 6px 0; text-align: right;">{depth_str}</td>
        </tr>
      </table>
      {failures_html}
    </div>
    """


def compose_daily_html(instruments: list[dict]) -> str:
    """Compose the full daily report HTML email.

    Args:
        instruments: List of instrument data dicts from _gather_instrument_data().

    Returns:
        Complete HTML email string.
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%B %d, %Y")

    cards = "\n".join(_instrument_card_html(inst) for inst in instruments)

    # Overall status summary
    n_yellow = sum(1 for i in instruments if i["status"] == "yellow")
    n_red = sum(1 for i in instruments if i["status"] == "red")

    if n_red > 0:
        overall_color = "#ef4444"
        overall_text = f"{n_red} instrument(s) need attention"
    elif n_yellow > 0:
        overall_color = "#eab308"
        overall_text = f"{n_yellow} instrument(s) to watch"
    else:
        overall_color = "#22c55e"
        overall_text = "All instruments OK"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>STAN Daily QC Report</title>
</head>
<body style="margin: 0; padding: 0; background: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
  <div style="max-width: 600px; margin: 0 auto; padding: 24px 16px;">

    <!-- Header -->
    <div style="text-align: center; padding: 24px 0 16px;">
      <h1 style="margin: 0; font-size: 28px; font-weight: 800; letter-spacing: -0.5px;">
        <span style="color: #3b82f6;">STAN</span>
      </h1>
      <p style="margin: 4px 0 0; color: #64748b; font-size: 12px; letter-spacing: 1px; text-transform: uppercase;">
        Standardized Proteomic Throughput Analyzer
      </p>
    </div>

    <!-- Date + Summary -->
    <div style="text-align: center; padding: 12px 0 24px;">
      <p style="margin: 0; color: #94a3b8; font-size: 14px;">Daily QC Report &mdash; {date_str}</p>
      <p style="margin: 8px 0 0; color: {overall_color}; font-size: 16px; font-weight: 600;">
        {overall_text}
      </p>
    </div>

    <!-- Instrument Cards -->
    {cards}

    <!-- Footer -->
    <div style="text-align: center; padding: 24px 0 16px; border-top: 1px solid #1e293b;">
      <p style="margin: 0; color: #475569; font-size: 12px;">
        <a href="https://community.stan-proteomics.org" style="color: #3b82f6; text-decoration: none;">
          community.stan-proteomics.org
        </a>
      </p>
      <p style="margin: 8px 0 0; color: #334155; font-size: 11px;">
        STAN v{__version__} &mdash; Know Your Instrument
      </p>
    </div>

  </div>
</body>
</html>"""


def compose_weekly_html(instruments: list[dict]) -> str:
    """Compose the weekly summary HTML email.

    Includes everything in the daily report plus aggregate stats for the week.
    """
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%b %d")
    week_end = now.strftime("%b %d, %Y")

    cards = "\n".join(_instrument_card_html(inst) for inst in instruments)

    # Weekly summary stats
    total_runs = sum(i["n_runs_period"] for i in instruments)
    total_failures = sum(i["n_failures"] for i in instruments)
    total_submitted = 0
    for inst in instruments:
        total_submitted += sum(
            1 for r in inst.get("period_runs", [])
            if r.get("submitted_to_benchmark")
        )

    # Maintenance events
    all_events = []
    for inst in instruments:
        for evt in inst.get("period_events", []):
            all_events.append((inst["name"], evt))

    events_html = ""
    if all_events:
        events_html = (
            '<div style="background: #1e293b; border-radius: 10px; padding: 20px; '
            'margin-bottom: 16px; border: 1px solid #334155;">'
            '<h3 style="margin: 0 0 12px; color: #f1f5f9; font-size: 16px;">'
            'Maintenance Events This Week</h3>'
        )
        for inst_name, evt in all_events:
            evt_type = escape(evt.get("event_type", "").replace("_", " "))
            evt_notes = escape(evt.get("notes", "")[:100])
            events_html += (
                f'<div style="padding: 6px 0; color: #94a3b8; font-size: 13px; '
                f'border-bottom: 1px solid #334155;">'
                f'<span style="color: #e2e8f0; font-weight: 600;">{escape(inst_name)}</span>'
                f' &mdash; {evt_type}'
                f'{f": {evt_notes}" if evt_notes else ""}'
                f'</div>'
            )
        events_html += "</div>"

    # Overall status
    n_red = sum(1 for i in instruments if i["status"] == "red")
    n_yellow = sum(1 for i in instruments if i["status"] == "yellow")
    if n_red > 0:
        overall_color = "#ef4444"
        overall_text = f"{n_red} instrument(s) need attention"
    elif n_yellow > 0:
        overall_color = "#eab308"
        overall_text = f"{n_yellow} instrument(s) to watch"
    else:
        overall_color = "#22c55e"
        overall_text = "All instruments OK"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>STAN Weekly QC Summary</title>
</head>
<body style="margin: 0; padding: 0; background: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
  <div style="max-width: 600px; margin: 0 auto; padding: 24px 16px;">

    <!-- Header -->
    <div style="text-align: center; padding: 24px 0 16px;">
      <h1 style="margin: 0; font-size: 28px; font-weight: 800; letter-spacing: -0.5px;">
        <span style="color: #3b82f6;">STAN</span>
      </h1>
      <p style="margin: 4px 0 0; color: #64748b; font-size: 12px; letter-spacing: 1px; text-transform: uppercase;">
        Standardized Proteomic Throughput Analyzer
      </p>
    </div>

    <!-- Date + Summary -->
    <div style="text-align: center; padding: 12px 0 24px;">
      <p style="margin: 0; color: #94a3b8; font-size: 14px;">
        Weekly QC Summary &mdash; {week_start} to {week_end}
      </p>
      <p style="margin: 8px 0 0; color: {overall_color}; font-size: 16px; font-weight: 600;">
        {overall_text}
      </p>
    </div>

    <!-- Weekly Stats Bar -->
    <div style="display: flex; justify-content: space-around; padding: 16px; margin-bottom: 16px;
                background: #1e293b; border-radius: 10px; border: 1px solid #334155;">
      <div style="text-align: center;">
        <div style="font-size: 24px; font-weight: 700; color: #e2e8f0;">{total_runs}</div>
        <div style="font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px;">
          QC Runs
        </div>
      </div>
      <div style="text-align: center;">
        <div style="font-size: 24px; font-weight: 700; color: {'#ef4444' if total_failures > 0 else '#22c55e'};">
          {total_failures}
        </div>
        <div style="font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px;">
          Failures
        </div>
      </div>
      <div style="text-align: center;">
        <div style="font-size: 24px; font-weight: 700; color: #3b82f6;">{total_submitted}</div>
        <div style="font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px;">
          Submitted
        </div>
      </div>
    </div>

    <!-- Instrument Cards -->
    {cards}

    <!-- Maintenance Events -->
    {events_html}

    <!-- Footer -->
    <div style="text-align: center; padding: 24px 0 16px; border-top: 1px solid #1e293b;">
      <p style="margin: 0; color: #475569; font-size: 12px;">
        <a href="https://community.stan-proteomics.org" style="color: #3b82f6; text-decoration: none;">
          community.stan-proteomics.org
        </a>
      </p>
      <p style="margin: 8px 0 0; color: #334155; font-size: 11px;">
        STAN v{__version__} &mdash; Know Your Instrument
      </p>
    </div>

  </div>
</body>
</html>"""


# ── Sending ──────────────────────────────────────────────────────


def _send_email(to: str, subject: str, html: str) -> dict:
    """Send an email via the Resend API.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        html: HTML body.

    Returns:
        Resend API response dict.

    Raises:
        RuntimeError: If the Resend API returns an error.
    """
    api_key = _get_resend_api_key()

    payload = json.dumps({
        "from": FROM_ADDRESS,
        "to": [to],
        "subject": subject,
        "html": html,
    }).encode()

    req = urllib.request.Request(
        RESEND_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"STAN/{__version__}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
            detail = body.get("message", str(exc))
        except Exception:
            detail = str(exc)
        raise RuntimeError(f"Resend API error: {detail}") from exc


def send_daily_report(to: str | None = None) -> dict:
    """Compose and send the daily QC report email.

    Args:
        to: Override recipient address. Falls back to config.

    Returns:
        Resend API response dict.
    """
    if to is None:
        cfg = get_email_config()
        to = cfg.get("to", "")
    if not to:
        raise ValueError("No recipient email configured. Run: stan email-report --enable --to YOUR_EMAIL")

    instruments = _gather_instrument_data(period_days=1)
    if not instruments:
        raise ValueError("No instruments configured. Run: stan setup")

    html = compose_daily_html(instruments)
    subject = f"STAN Daily QC Report -- {datetime.now(timezone.utc).strftime('%B %d, %Y')}"

    return _send_email(to, subject, html)


def send_weekly_report(to: str | None = None) -> dict:
    """Compose and send the weekly QC summary email.

    Args:
        to: Override recipient address. Falls back to config.

    Returns:
        Resend API response dict.
    """
    if to is None:
        cfg = get_email_config()
        to = cfg.get("to", "")
    if not to:
        raise ValueError("No recipient email configured. Run: stan email-report --enable --to YOUR_EMAIL")

    instruments = _gather_instrument_data(period_days=7)
    if not instruments:
        raise ValueError("No instruments configured. Run: stan setup")

    html = compose_weekly_html(instruments)
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%b %d")
    subject = f"STAN Weekly QC Summary -- {week_start} to {now.strftime('%b %d, %Y')}"

    return _send_email(to, subject, html)


def send_test_email(to: str | None = None) -> dict:
    """Send a simple test email to verify Resend API connectivity.

    Args:
        to: Recipient email address. Falls back to config.

    Returns:
        Resend API response dict.
    """
    if to is None:
        cfg = get_email_config()
        to = cfg.get("to", "")
    if not to:
        raise ValueError("No recipient email configured. Run: stan email-report --enable --to YOUR_EMAIL")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin: 0; padding: 0; background: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
  <div style="max-width: 600px; margin: 0 auto; padding: 40px 16px; text-align: center;">
    <h1 style="color: #3b82f6; font-size: 28px; font-weight: 800; margin: 0;">STAN</h1>
    <p style="color: #64748b; font-size: 12px; letter-spacing: 1px; text-transform: uppercase; margin: 4px 0 24px;">
      Standardized Proteomic Throughput Analyzer
    </p>
    <div style="background: #1e293b; border-radius: 10px; padding: 24px; border: 1px solid #334155;">
      <p style="color: #22c55e; font-size: 18px; font-weight: 600; margin: 0 0 8px;">
        Email delivery is working
      </p>
      <p style="color: #94a3b8; font-size: 14px; margin: 0;">
        STAN v{__version__} can send you daily QC reports at this address.
      </p>
    </div>
    <p style="color: #334155; font-size: 11px; margin-top: 24px;">
      Sent at {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    </p>
  </div>
</body>
</html>"""

    return _send_email(to, "STAN Test Email", html)


# ── Scheduled task installation ──────────────────────────────────


def install_scheduled_task(daily_time: str = "07:00") -> str:
    """Install a system-level scheduled task to send the daily report.

    On Windows: creates a Windows Scheduled Task via schtasks.exe.
    On Linux/macOS: outputs a cron line the user can add.

    Args:
        daily_time: Time in HH:MM format for the daily report.

    Returns:
        Human-readable message describing what was done.
    """
    system = platform.system()
    stan_cmd = "stan email-report --send"

    if system == "Windows":
        task_name = "STAN_Daily_QC_Report"

        # Build schtasks command
        cmd = [
            "schtasks.exe",
            "/Create",
            "/TN", task_name,
            "/TR", stan_cmd,
            "/SC", "DAILY",
            "/ST", daily_time,
            "/F",  # force overwrite if exists
        ]

        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            return (
                f"Windows Scheduled Task '{task_name}' created.\n"
                f"  Runs daily at {daily_time}.\n"
                f"  Command: {stan_cmd}\n"
                f"  To remove: schtasks /Delete /TN {task_name} /F"
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Failed to create scheduled task: {exc.stderr}"
            ) from exc
        except FileNotFoundError:
            raise RuntimeError(
                "schtasks.exe not found. Are you on Windows?"
            )

    else:
        # Linux / macOS -- output a cron line
        hour, minute = daily_time.split(":")
        cron_line = f"{minute} {hour} * * * {stan_cmd}"
        return (
            f"Add this line to your crontab (crontab -e):\n\n"
            f"  {cron_line}\n\n"
            f"To also send a weekly report on Mondays at {daily_time}:\n\n"
            f"  {minute} {hour} * * 1 stan email-report --send-weekly"
        )
