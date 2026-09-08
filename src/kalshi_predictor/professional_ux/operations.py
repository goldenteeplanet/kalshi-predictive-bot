from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.professional_ux.service import (
    build_shell_status_context,
    build_today_workspace,
    phase_3x_status_card,
)
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.workspace_guard import build_workspace_consistency_guard


def build_ops_status(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build a read-only operator snapshot from existing authoritative services."""
    resolved = settings or get_settings()
    shell = build_shell_status_context(session, settings=resolved)
    phase_3x = phase_3x_status_card(session, settings=resolved)
    workspace = build_workspace_consistency_guard(settings=resolved)
    checks = [
        _check(
            "Runtime origin",
            _map_workspace_status(workspace["summary"]["status"]),
            workspace["ui_badge"]["description"],
            workspace["next_action"],
        ),
        _check(
            "Database",
            _map_shell_status(shell["system_status"]["code"]),
            shell["system_status"]["description"],
            "Run `kalshi-bot db-health` and resolve every failed check.",
        ),
        _check(
            "Data freshness",
            _map_shell_status(shell["market_freshness"]["code"]),
            shell["market_freshness"]["description"],
            "Refresh the relevant public-data owner; do not fabricate inputs.",
        ),
        _check(
            "Phase 3W certification",
            "GREEN" if phase_3x["phase_3w_status"] == "SYSTEM_PASS" else "BLOCKED",
            f"Current certification is {phase_3x['phase_3w_status']}.",
            "Complete Phase 3W certification before promoting Phase 3X.",
        ),
        _check(
            "Execution boundary",
            "DISABLED",
            "Phase 3X is an operator-console and reporting phase only.",
            "Keep live/demo execution and automatic promotion disabled.",
        ),
    ]
    return {
        "schema_version": "phase-3x-ops-status-v1",
        "generated_at": utc_now().isoformat(),
        "overall_status": _overall_status(checks),
        "environment": shell["environment"],
        "execution_mode": shell["execution_mode"],
        "checks": checks,
        "phase_3x": phase_3x,
        "live_trading_authorized": False,
    }


def write_morning_briefing(
    session: Session,
    *,
    output_path: Path = Path("reports/morning_briefing.md"),
    settings: Settings | None = None,
) -> Path:
    resolved = settings or get_settings()
    status = build_ops_status(session, settings=resolved)
    today = build_today_workspace(session, settings=resolved)
    lines = [
        "# Morning Briefing",
        "",
        f"Generated: `{status['generated_at']}`",
        f"Overall status: **{status['overall_status']}**",
        f"Execution mode: **{status['execution_mode']}**",
        "",
        "## Today's Summary",
        "",
        f"- Markets ranked: {today['candidate_count']}",
        f"- Forecasts generated: {today['forecast_count']}",
        f"- Paper trades: {today['paper_trade_count']}",
        f"- Decision: {today['decision_label']}",
        f"- Phase 3W: {today['phase_3w_status']}",
        "",
        "## Operations",
        "",
        *_render_checks(status["checks"]),
        "",
        "Live trading is not authorized by this report.",
    ]
    return _write_report(output_path, lines)


def write_daily_close(
    session: Session,
    *,
    output_path: Path = Path("reports/daily_close.md"),
    settings: Settings | None = None,
) -> Path:
    resolved = settings or get_settings()
    status = build_ops_status(session, settings=resolved)
    today = build_today_workspace(session, settings=resolved)
    lines = [
        "# Daily Close",
        "",
        f"Generated: `{status['generated_at']}`",
        f"Overall status: **{status['overall_status']}**",
        "",
        "## Activity",
        "",
        f"- Markets ranked: {today['candidate_count']}",
        f"- Forecasts generated: {today['forecast_count']}",
        f"- Paper trades: {today['paper_trade_count']}",
        f"- Risk blocks: {today['blocked_count']}",
        f"- Risk reductions: {today['reduced_count']}",
        "",
        "## Closing Checks",
        "",
        *_render_checks(status["checks"]),
        "",
        "This is a paper/research operations report. Live trading is not authorized.",
    ]
    return _write_report(output_path, lines)


def _check(name: str, status: str, problem: str, next_action: str) -> dict[str, str]:
    return {
        "name": name,
        "status": status,
        "what_is_wrong": problem if status not in {"GREEN", "DISABLED"} else "Nothing reported.",
        "why_it_matters": problem,
        "next_action": next_action,
    }


def _map_workspace_status(value: str) -> str:
    return {"PASS": "GREEN", "WARNING": "YELLOW", "FAIL": "RED"}.get(value, "NEEDS DATA")


def _map_shell_status(value: str) -> str:
    normalized = value.lower()
    if normalized in {"healthy", "fresh", "ok"}:
        return "GREEN"
    if normalized in {"degraded", "stale", "warning"}:
        return "YELLOW"
    if normalized in {"failed", "error"}:
        return "RED"
    return "NEEDS DATA"


def _overall_status(checks: list[dict[str, str]]) -> str:
    states = {item["status"] for item in checks}
    if states & {"RED", "BLOCKED"}:
        return "BLOCKED"
    if states & {"YELLOW", "STALE", "NEEDS DATA"}:
        return "YELLOW"
    return "GREEN"


def _render_checks(checks: list[dict[str, str]]) -> list[str]:
    return [
        f"- **{item['name']} — {item['status']}**: {item['why_it_matters']} "
        f"Next: {item['next_action']}"
        for item in checks
    ]


def _write_report(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
