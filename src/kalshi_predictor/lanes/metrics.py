from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    AutopilotMetric,
    AutopilotOpportunity,
    AutopilotPaperTrade,
    LearningMetric,
    LearningOpportunity,
    LearningPaperTrade,
    ModelConfidenceScore,
    Settlement,
)
from kalshi_predictor.lanes.repository import insert_autopilot_metric, insert_learning_metric
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def refresh_learning_metrics(
    session: Session,
    *,
    settings: Settings | None = None,
    window_days: int = 30,
) -> LearningMetric:
    resolved_settings = settings or get_settings()
    since = utc_now() - timedelta(days=window_days)
    trade_rows = list(
        session.execute(
            select(LearningPaperTrade, Settlement)
            .join(Settlement, LearningPaperTrade.ticker == Settlement.ticker, isouter=True)
            .where(LearningPaperTrade.created_at >= since)
        ).all()
    )
    paper_stats = _paper_stats(trade_rows)
    opportunities = _count_since(session, LearningOpportunity, since)
    confidence = _learning_confidence(
        settled=paper_stats["settled_trade_count"],
        target=resolved_settings.learning_target_settled_trades,
    )
    return insert_learning_metric(
        session,
        {
            "window_days": window_days,
            "opportunities_found": opportunities,
            "paper_trades_created": len(trade_rows),
            "settled_trade_count": paper_stats["settled_trade_count"],
            "win_rate": paper_stats["win_rate"],
            "roi_on_exposure": paper_stats["roi_on_exposure"],
            "total_pnl": paper_stats["total_pnl"],
            "learning_confidence": confidence,
            "notes": "Learning metrics use only learning lane paper trades.",
            "raw_json": paper_stats,
        },
    )


def refresh_autopilot_metrics(
    session: Session,
    *,
    settings: Settings | None = None,
    window_days: int = 30,
) -> AutopilotMetric:
    resolved_settings = settings or get_settings()
    since = utc_now() - timedelta(days=window_days)
    trade_rows = list(
        session.execute(
            select(AutopilotPaperTrade, Settlement)
            .join(Settlement, AutopilotPaperTrade.ticker == Settlement.ticker, isouter=True)
            .where(AutopilotPaperTrade.created_at >= since)
        ).all()
    )
    paper_stats = _paper_stats(trade_rows)
    opportunities = _count_since(session, AutopilotOpportunity, since)
    confidence = _current_model_confidence(session, resolved_settings.autopilot_model)
    return insert_autopilot_metric(
        session,
        {
            "window_days": window_days,
            "opportunities_found": opportunities,
            "dry_run_orders": sum(
                1 for trade, _settlement in trade_rows if trade.status == "DRY_RUN"
            ),
            "settled_trade_count": paper_stats["settled_trade_count"],
            "win_rate": paper_stats["win_rate"],
            "roi_on_exposure": paper_stats["roi_on_exposure"],
            "total_pnl": paper_stats["total_pnl"],
            "current_confidence": confidence,
            "notes": "Autopilot metrics use only autopilot lane dry-run/demo trades.",
            "raw_json": paper_stats,
        },
    )


def _paper_stats(rows: list[tuple[Any, Settlement | None]]) -> dict[str, Any]:
    settled = []
    for trade, settlement in rows:
        if settlement is None or settlement.result not in {"yes", "no"}:
            continue
        pnl = _trade_pnl(trade, settlement)
        exposure = (to_decimal(trade.price) or Decimal("0")) * Decimal(trade.quantity)
        settled.append({"pnl": pnl, "exposure": exposure, "win": Decimal(int(pnl > 0))})
    total_pnl = sum((row["pnl"] for row in settled), Decimal("0"))
    exposure = sum((row["exposure"] for row in settled), Decimal("0"))
    wins = sum((row["win"] for row in settled), Decimal("0"))
    return {
        "settled_trade_count": len(settled),
        "win_rate": wins / Decimal(len(settled)) if settled else None,
        "roi_on_exposure": total_pnl / exposure if exposure > 0 else None,
        "total_pnl": total_pnl if settled else None,
    }


def _trade_pnl(trade: Any, settlement: Settlement) -> Decimal:
    price = to_decimal(trade.price) or Decimal("0")
    quantity = Decimal(trade.quantity)
    cost = price * quantity
    if trade.side == BUY_YES:
        payout = quantity if settlement.result == "yes" else Decimal("0")
    elif trade.side == BUY_NO:
        payout = quantity if settlement.result == "no" else Decimal("0")
    else:
        payout = Decimal("0")
    return payout - cost


def _count_since(session: Session, table: Any, since: Any) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(table).where(table.created_at >= since)
        )
        or 0
    )


def _learning_confidence(*, settled: int, target: int) -> Decimal:
    if target <= 0:
        return Decimal("0")
    return min(Decimal("100"), Decimal(settled) / Decimal(target) * Decimal("100"))


def _current_model_confidence(session: Session, model_name: str) -> Decimal | None:
    row = session.scalar(
        select(ModelConfidenceScore)
        .where(ModelConfidenceScore.model_name == model_name)
        .order_by(desc(ModelConfidenceScore.generated_at), desc(ModelConfidenceScore.id))
        .limit(1)
    )
    return to_decimal(row.confidence_score if row else None)
