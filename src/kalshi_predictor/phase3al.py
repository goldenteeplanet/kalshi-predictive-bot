from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3aa import build_settlement_eta_schedule
from kalshi_predictor.phase3ab import build_learning_governor
from kalshi_predictor.phase3ak import build_multi_leg_component_provenance
from kalshi_predictor.utils.time import utc_now

PHASE_3AL_VERSION = "phase3al_v1"


@dataclass(frozen=True)
class Phase3ALArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def build_phase3al_resume_plan(
    session: Session,
    *,
    settings: Settings | None = None,
    model_name: str = "ensemble_v2",
    limit: int = 500,
) -> dict[str, Any]:
    """Build the paper-only Learning Mode resume decision.

    Phase 3AL does not create paper trades. It decides whether the next paper-learning
    cycle should resume after the daily cap resets, and which candidate classes must
    stay excluded.
    """
    resolved = settings or get_settings()
    session.flush()
    now = utc_now()
    daily_count = _daily_paper_trade_count(session, now=now)
    cap_remaining = max(0, resolved.learning_max_daily_paper_trades - daily_count)
    governor = build_learning_governor(
        session,
        settings=resolved,
        model_name=model_name,
        limit=limit,
    )
    settlement = build_settlement_eta_schedule(session, limit=limit)
    multileg = build_multi_leg_component_provenance(
        session,
        limit=limit,
        include_single_leg=False,
    )
    blocked_multileg = int(multileg.get("summary", {}).get("blocked_multi_leg_markets", 0) or 0)
    fast_count = governor["summary"]["fast_settlement_candidates"]
    can_resume = cap_remaining > 0 and fast_count > 0
    blockers = []
    if cap_remaining <= 0:
        blockers.append("daily_cap_reached")
    if fast_count <= 0:
        blockers.append("no_fast_settlement_candidates")
    if blocked_multileg:
        blockers.append("multi_leg_unsafe_candidates_excluded")
    return {
        "generated_at": now.isoformat(),
        "phase": "3AL",
        "phase_version": PHASE_3AL_VERSION,
        "mode": "PAPER_ONLY_LEARNING_RESUME_DECISION",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "model_name": model_name,
        "daily_cap": {
            "paper_trades_today": daily_count,
            "max_daily_paper_trades": resolved.learning_max_daily_paper_trades,
            "remaining_today": cap_remaining,
            "next_reset_utc": _next_midnight_utc(now).isoformat(),
        },
        "resume_decision": {
            "can_resume_now": can_resume,
            "blockers": blockers,
            "resume_after_cap_reset": daily_count >= resolved.learning_max_daily_paper_trades,
            "paper_only": True,
            "demo_execution_blocked": resolved.learning_block_demo_execution,
            "live_execution_blocked": resolved.learning_block_live_execution,
            "exact_ticker_settlement_policy": "EXACT_TICKER_ONLY",
        },
        "fast_settlement_summary": governor["summary"],
        "settlement_summary": settlement["summary"],
        "multi_leg_gate_summary": multileg["summary"],
        "top_fast_candidates": governor["top_fast_candidates"][:25],
        "excluded_multi_leg_examples": multileg.get("blocked_examples", [])[:25],
        "recommended_env": _recommended_env(resolved),
        "recommended_next_action": _next_action(can_resume, blockers),
    }


def write_phase3al_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3al"),
    settings: Settings | None = None,
    model_name: str = "ensemble_v2",
    limit: int = 500,
) -> Phase3ALArtifactSet:
    payload = build_phase3al_resume_plan(
        session,
        settings=settings,
        model_name=model_name,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3al_learning_resume.json"
    markdown_path = output_dir / "phase3al_learning_resume.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3ALArtifactSet(output_dir, json_path, markdown_path)


def _daily_paper_trade_count(session: Session, *, now) -> int:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        session.scalar(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.created_at >= today)
        )
        or 0
    )


def _next_midnight_utc(now):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=1)


def _recommended_env(settings: Settings) -> dict[str, str]:
    return {
        "LEARNING_MODE": "true",
        "EXECUTION_ENABLED": "false",
        "LEARNING_PRIORITIZE_FAST_SETTLEMENT": "true",
        "LEARNING_MAX_DAYS_TO_SETTLEMENT": "1",
        "LEARNING_CANDIDATE_SCAN_LIMIT": str(settings.learning_candidate_scan_limit),
        "LEARNING_BLOCK_DEMO_EXECUTION": "true",
        "LEARNING_BLOCK_LIVE_EXECUTION": "true",
    }


def _next_action(can_resume: bool, blockers: list[str]) -> str:
    if can_resume:
        return (
            "Resume a short paper-only learning cycle from 0-24h candidates, then rerun "
            "settlement diagnostics."
        )
    if "daily_cap_reached" in blockers:
        return "Wait for the daily cap reset before resuming paper-only learning."
    if "no_fast_settlement_candidates" in blockers:
        return "Collect fresh markets and rankings until 0-24h candidates appear."
    return "Keep paper learning paused until eligibility blockers clear."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AL Settlement-Aware Learning Resume",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "",
        "## Resume Decision",
        "",
    ]
    for key, value in payload["resume_decision"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Daily Cap", ""])
    for key, value in payload["daily_cap"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Recommended Environment", "", "```bash"])
    for key, value in payload["recommended_env"].items():
        lines.append(f"export {key}={value}")
    lines.extend(
        [
            "```",
            "",
            "## Top Fast Candidates",
            "",
            "| Ticker | Category | ETA | Score |",
            "| --- | --- | --- | ---: |",
        ]
    )
    for row in payload["top_fast_candidates"][:20]:
        lines.append(
            f"| {row['ticker']} | {row['category']} | {row['eta_bucket']} | "
            f"{row['governor_score']} |"
        )
    if not payload["top_fast_candidates"]:
        lines.append("| none |  |  |  |")
    lines.extend(
        [
            "",
            "## Excluded Multi-Leg Examples",
            "",
            "| Ticker | Legs | Reason |",
            "| --- | ---: | --- |",
        ]
    )
    for row in payload["excluded_multi_leg_examples"][:20]:
        lines.append(
            f"| `{row['ticker']}` | {row['sports_leg_count']} | "
            f"{row['blocking_reason']} |"
        )
    if not payload["excluded_multi_leg_examples"]:
        lines.append("| none | 0 |  |")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)
