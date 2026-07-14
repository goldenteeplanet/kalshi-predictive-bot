from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import Forecast, ModelConfidenceScore, PaperOrder, PaperPnl
from kalshi_predictor.learning.diagnostics import build_learning_diagnostics
from kalshi_predictor.opportunities.link_audit import (
    OpportunityLinkAuditArtifacts,
    write_opportunity_link_audit,
)
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.paper.settlement_reconciliation import (
    PAPER_ONLY_SAFETY,
    build_paper_settlement_reconciliation,
)
from kalshi_predictor.reinforcement_learning.repository import rl_status
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE_3AO_VERSION = "phase3ao_v1"


@dataclass(frozen=True)
class Phase3AOArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def build_phase3ao_learning_reward_pipeline(
    session: Session,
    *,
    scan_limit: int = 500,
) -> dict[str, Any]:
    """Summarize paper settlement rewards feeding confidence and offline RL."""
    session.flush()
    reconciliation = build_paper_settlement_reconciliation(session, limit=scan_limit)
    diagnostics = build_learning_diagnostics(
        session,
        scan_limit=scan_limit,
        suggest_thresholds=True,
    )
    pnl_rows = _latest_pnl_rows(session)
    confidence_rows = _latest_confidence_rows(session)
    reward = _reward_summary(session, pnl_rows)
    rl = rl_status(session)
    can_train = reward["settled_trade_count"] > 0
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AO",
        "phase_version": PHASE_3AO_VERSION,
        "mode": "PAPER_ONLY_LEARNING_REWARD_PIPELINE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "settlement_policy": "EXACT_TICKER_ONLY",
        "summary": {
            "filled_paper_orders": _filled_order_count(session),
            "settled_trade_count": reward["settled_trade_count"],
            "realized_pnl": reward["realized_pnl"],
            "roi": reward["roi"],
            "model_confidence_rows": len(confidence_rows),
            "rl_run_count": rl.get("run_count", 0),
            "offline_rl_ready": can_train,
        },
        "reward_metrics": reward,
        "latest_model_confidence": confidence_rows,
        "rl_status": rl,
        "learning_funnel": diagnostics["funnel"],
        "settlement_reconciliation_summary": reconciliation["summary"],
        "recommended_next_action": _next_action(can_train, reward),
        "next_commands": _next_commands(can_train),
    }


def write_phase3ao_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ao"),
    scan_limit: int = 500,
) -> Phase3AOArtifactSet:
    payload = build_phase3ao_learning_reward_pipeline(session, scan_limit=scan_limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ao_learning_reward_pipeline.json"
    markdown_path = output_dir / "phase3ao_learning_reward_pipeline.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AOArtifactSet(output_dir, json_path, markdown_path)


def write_phase3ao_opportunity_link_audit(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ao"),
    limit: int = 2000,
) -> OpportunityLinkAuditArtifacts:
    return write_opportunity_link_audit(session, output_dir=output_dir, limit=limit)


def _filled_order_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.status == ORDER_FILLED)
        )
        or 0
    )


def _latest_pnl_rows(session: Session) -> list[PaperPnl]:
    rows = list(
        session.scalars(select(PaperPnl).order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id)))
    )
    seen: set[str] = set()
    latest: list[PaperPnl] = []
    for row in rows:
        if row.ticker in seen:
            continue
        seen.add(row.ticker)
        latest.append(row)
    return latest


def _latest_confidence_rows(session: Session) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(
            select(ModelConfidenceScore)
            .order_by(desc(ModelConfidenceScore.generated_at), desc(ModelConfidenceScore.id))
            .limit(20)
        )
    )
    return [
        {
            "model_name": row.model_name,
            "category": row.category,
            "settled_trade_count": row.settled_trade_count,
            "roi_on_exposure": row.roi_on_exposure,
            "brier_score": row.brier_score,
            "win_rate": row.win_rate,
            "confidence_label": row.confidence_label,
            "status": row.status,
            "generated_at": row.generated_at.isoformat(),
        }
        for row in rows
    ]


def _reward_summary(session: Session, pnl_rows: list[PaperPnl]) -> dict[str, Any]:
    settled = [row for row in pnl_rows if row.settlement_result]
    realized = sum((to_decimal(row.realized_pnl) or Decimal("0")) for row in settled)
    exposure = _settled_exposure(session, {row.ticker for row in settled})
    wins = sum(1 for row in settled if (to_decimal(row.realized_pnl) or Decimal("0")) > 0)
    losses = sum(1 for row in settled if (to_decimal(row.realized_pnl) or Decimal("0")) < 0)
    roi = realized / exposure if exposure > 0 else Decimal("0")
    return {
        "settled_trade_count": len(settled),
        "wins": wins,
        "losses": losses,
        "realized_pnl": str(realized.quantize(Decimal("0.0001"))),
        "settled_exposure": str(exposure.quantize(Decimal("0.0001"))),
        "roi": str(roi.quantize(Decimal("0.0001"))),
        "brier_available": int(
            session.scalar(
                select(func.count())
                .select_from(Forecast)
                .where(Forecast.model_name.is_not(None))
            )
            or 0
        ),
    }


def _settled_exposure(session: Session, tickers: set[str]) -> Decimal:
    if not tickers:
        return Decimal("0")
    orders = list(session.scalars(select(PaperOrder).where(PaperOrder.ticker.in_(tickers))))
    total = Decimal("0")
    for order in orders:
        total += (to_decimal(order.market_price) or Decimal("0")) * Decimal(order.quantity)
    return total


def _next_action(can_train: bool, reward: dict[str, Any]) -> str:
    if can_train:
        return "Run model-confidence and offline RL evaluation from exact settled paper rewards."
    return (
        "Keep settlement watcher active and avoid RL training claims until exact ticker "
        "settlements produce realized paper rewards."
    )


def _next_commands(can_train: bool) -> list[str]:
    commands = [
        "kalshi-bot phase3aa-realize --sync-settlements --output-dir reports/phase3aa",
        "kalshi-bot model-confidence",
    ]
    if can_train:
        commands.append("kalshi-bot rl-evaluate --enable-research")
    commands.append("kalshi-bot phase3ao-learning-reward-pipeline")
    return commands


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AO Learning Reward Pipeline",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Settlement policy: {payload['settlement_policy']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Latest Model Confidence",
            "",
            "| Model | Category | Settled | ROI | Brier | Win rate | Label |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in payload["latest_model_confidence"]:
        lines.append(
            f"| {row['model_name']} | {row['category']} | {row['settled_trade_count']} | "
            f"{row['roi_on_exposure']} | {row['brier_score']} | {row['win_rate']} | "
            f"{row['confidence_label']} |"
        )
    if not payload["latest_model_confidence"]:
        lines.append("| none |  | 0 |  |  |  |  |")
    lines.extend(["", "## Next Commands", "", "```bash"])
    lines.extend(payload["next_commands"])
    lines.extend(
        ["```", "", "## Recommended Next Action", "", payload["recommended_next_action"], ""]
    )
    return "\n".join(lines)
