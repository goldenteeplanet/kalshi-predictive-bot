from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.lanes.metrics import refresh_learning_metrics
from kalshi_predictor.lanes.repository import (
    insert_learning_opportunity,
    insert_learning_trade_for_order,
)
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.learning.targets import generate_learning_targets
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.paper.simulator import run_paper_trading
from kalshi_predictor.utils.decimals import to_decimal


@dataclass(frozen=True)
class AccelerateLearningResult:
    targets_scanned: int
    targets_inserted: int
    fast_settling_categories: list[str]
    opportunities_found: int
    learning_opportunities_inserted: int
    paper_trades_created: int
    learning_paper_trades_inserted: int
    learning_metric_id: int
    recommendation: str


def accelerate_learning(
    session: Session,
    *,
    settings: Settings | None = None,
    model_name: str = "ensemble_v2",
    limit: int = 100,
) -> AccelerateLearningResult:
    base_settings = settings or get_settings()
    learning_settings = learning_paper_settings(
        base_settings.model_copy(
            update={
                "learning_mode": True,
                "execution_enabled": False,
                "execution_dry_run": True,
                "autopilot_dry_run": True,
            }
        )
    )
    opportunities = scan_opportunities(
        session,
        model_name=model_name,
        limit=limit,
        settings=learning_settings,
        min_edge=learning_settings.learning_min_edge,
        min_score=learning_settings.learning_min_opportunity_score,
    )
    targets = generate_learning_targets(
        session,
        settings=learning_settings,
        model_name=model_name,
        limit=limit,
        persist=True,
    )
    categories = _fast_settling_categories(targets.targets)

    target_by_ticker = {str(row["ticker"]): row for row in targets.targets}
    learning_opportunities_inserted = 0
    for opportunity in opportunities.opportunities:
        target = target_by_ticker.get(str(opportunity["ticker"]), {})
        insert_learning_opportunity(
            session,
            {
                **opportunity,
                "source": "accelerate-learning",
                "settlement_speed_score": target.get("settlement_speed_score"),
            },
        )
        learning_opportunities_inserted += 1

    before_order_ids = _paper_order_ids(session)
    paper_summary = run_paper_trading(
        session,
        settings=learning_settings,
        model_name=model_name,
    )
    new_orders = _new_paper_orders(session, before_order_ids)
    learning_paper_trades_inserted = 0
    for order in new_orders:
        insert_learning_trade_for_order(session, order, source="accelerate-learning")
        learning_paper_trades_inserted += 1

    metric = refresh_learning_metrics(session, settings=learning_settings)
    return AccelerateLearningResult(
        targets_scanned=targets.scanned,
        targets_inserted=targets.inserted,
        fast_settling_categories=categories,
        opportunities_found=opportunities.opportunities_detected,
        learning_opportunities_inserted=learning_opportunities_inserted,
        paper_trades_created=paper_summary.orders_created,
        learning_paper_trades_inserted=learning_paper_trades_inserted,
        learning_metric_id=metric.id,
        recommendation=_recommendation(
            targets_inserted=targets.inserted,
            paper_trades_created=paper_summary.orders_created,
            categories=categories,
        ),
    )


def _paper_order_ids(session: Session) -> set[int]:
    return {int(order_id) for order_id in session.scalars(select(PaperOrder.id))}


def _new_paper_orders(session: Session, before_ids: set[int]) -> list[PaperOrder]:
    rows = session.scalars(select(PaperOrder).order_by(PaperOrder.created_at, PaperOrder.id))
    return [row for row in rows if row.id is not None and row.id not in before_ids]


def _fast_settling_categories(targets: list[dict[str, Any]]) -> list[str]:
    categories: dict[str, Decimal] = {}
    for target in targets:
        category = str(target.get("category") or "general")
        speed = to_decimal(target.get("settlement_speed_score")) or Decimal("0")
        categories[category] = max(speed, categories.get(category, Decimal("0")))
    return [
        category
        for category, _speed in sorted(
            categories.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ][:5]


def _recommendation(
    *,
    targets_inserted: int,
    paper_trades_created: int,
    categories: list[str],
) -> str:
    if paper_trades_created:
        return (
            f"Created {paper_trades_created} learning-lane paper trade(s). "
            "Let them settle before trusting metrics."
        )
    if targets_inserted:
        return (
            "Learning targets exist but no new paper trades were created; check duplicate "
            "orders, edge thresholds, and per-market position caps."
        )
    if categories:
        return "Fast-settling categories were found, but no qualifying learning targets exist yet."
    return "Collect fresh data, forecast ensemble_v2, then run accelerate-learning again."
