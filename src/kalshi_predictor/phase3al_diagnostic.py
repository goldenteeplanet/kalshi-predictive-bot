from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3aj_gap_closure import build_paper_trade_funnel
from kalshi_predictor.phase3am import build_phase3ay_due_settlement_diagnostic
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class Phase3ALDiagnosticArtifactSet:
    output_dir: Path
    diagnostic_path: Path
    executive_summary_path: Path
    next_actions_path: Path


def write_phase3al_diagnostic_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase_3al"),
    window_hours: int = 168,
    include_ui_state: bool = False,
    settings: Settings | None = None,
) -> Phase3ALDiagnosticArtifactSet:
    """Write the read-only paper-completion diagnostic bundle expected by the CLI."""
    funnel = build_paper_trade_funnel(
        session,
        window_hours=window_hours,
        replay_readonly=True,
        settings=settings,
    )
    settlement = build_phase3ay_due_settlement_diagnostic(session, limit=5)
    payload = {
        "generated_at": utc_now().isoformat(),
        "phase": "3AL",
        "phase_version": "phase3al_diagnostic_v1",
        "mode": "PAPER_ONLY_READ_ONLY_COMPLETION_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "paper_order_writes": False,
        "paper_fill_writes": False,
        "window_hours": window_hours,
        "include_ui_state": include_ui_state,
        "summary": {
            "rankings_reviewed": funnel["summary"]["rankings_reviewed"],
            "tradeable_paper_only": funnel["summary"]["tradeable_paper_only"],
            "paper_orders_created": 0,
            "due_paper_trades": settlement["summary"]["due_paper_trades"],
            "safe_to_apply_count": settlement["summary"]["safe_to_apply_count"],
            "status": "OK_EXPLAINED",
        },
        "paper_funnel": funnel,
        "settlement_diagnostic": settlement,
        "recommended_next_action": _next_action(funnel, settlement),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    diagnostic_path = output_dir / "phase_3al_diagnostic.json"
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    diagnostic_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    executive_summary_path.write_text(_render_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    return Phase3ALDiagnosticArtifactSet(
        output_dir,
        diagnostic_path,
        executive_summary_path,
        next_actions_path,
    )


def _next_action(funnel: dict[str, Any], settlement: dict[str, Any]) -> str:
    if int(settlement["summary"].get("safe_to_apply_count") or 0) > 0:
        return "Review exact-ticker dry-run settlement evidence; do not apply from Phase 3AL."
    if int(funnel["summary"].get("tradeable_paper_only") or 0) == 0:
        return "No paper trade is expected; keep the watch/funnel read-only until gates clear."
    return "Review tradeable paper-only rows before any separate paper-learning run."


def _render_summary(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AL Diagnostic Executive Summary",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Safety: {payload['paper_only_safety']}",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Phase 3AL Diagnostic Next Actions",
            "",
            f"- {payload['recommended_next_action']}",
            "- Keep live/demo execution blocked.",
            "- Use exact-ticker settlement evidence only.",
            "",
        ]
    )
