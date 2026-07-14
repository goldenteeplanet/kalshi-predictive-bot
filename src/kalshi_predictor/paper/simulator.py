from decimal import Decimal

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.paper.ledger import (
    create_paper_order,
    insert_paper_fill,
    mark_order_filled,
    update_position_for_fill,
)
from kalshi_predictor.paper.models import ORDER_OPEN, PaperRunSummary
from kalshi_predictor.paper.strategy import generate_paper_decisions
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def simulate_immediate_fill(
    session: Session,
    order: PaperOrder,
    *,
    settings: Settings | None = None,
) -> object | None:
    resolved_settings = settings or get_settings()
    if order.status != ORDER_OPEN:
        return None

    price = to_decimal(order.limit_price) or to_decimal(order.market_price)
    if price is None:
        return None
    quantity = int(order.quantity)
    fee = resolved_settings.paper_default_fee_per_contract * Decimal(quantity)
    filled_at = utc_now()
    fill = insert_paper_fill(
        session,
        order=order,
        price=price,
        quantity=quantity,
        fee=fee,
        filled_at=filled_at,
    )
    mark_order_filled(session, order, filled_at)
    update_position_for_fill(session, fill)
    return fill


def run_paper_trading(
    session: Session,
    *,
    settings: Settings | None = None,
    model_name: str | None = None,
) -> PaperRunSummary:
    resolved_settings = settings or get_settings()
    strategy_result = generate_paper_decisions(
        session,
        settings=resolved_settings,
        model_name=model_name,
    )
    orders_created = 0
    fills_created = 0

    for decision in strategy_result.decisions:
        order = create_paper_order(session, decision, settings=resolved_settings)
        if order is None:
            continue
        orders_created += 1
        fill = simulate_immediate_fill(session, order, settings=resolved_settings)
        if fill is not None:
            fills_created += 1

    return PaperRunSummary(
        forecasts_scanned=strategy_result.forecasts_scanned,
        decisions_generated=strategy_result.decisions_generated,
        orders_created=orders_created,
        fills_created=fills_created,
        skipped_due_to_edge=strategy_result.skipped_due_to_edge,
        skipped_due_to_risk_limits=strategy_result.skipped_due_to_risk_limits,
        duplicates_skipped=strategy_result.duplicates_skipped,
        candidate_scan_limit=strategy_result.candidate_scan_limit,
    )
