from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import Session, aliased

from kalshi_predictor.advanced_risk.repository import (
    attach_advanced_risk_decision_to_order,
    mark_reservation_filled_for_order,
)
from kalshi_predictor.advanced_risk.service import advanced_risk_decision_id
from kalshi_predictor.config import Settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    Forecast,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
    PaperPnl,
    PaperPosition,
    Settlement,
)
from kalshi_predictor.paper.models import (
    BUY_NO,
    BUY_YES,
    ORDER_FILLED,
    ORDER_OPEN,
    PaperDecision,
    PaperSummary,
)
from kalshi_predictor.position_sizing.repository import attach_position_sizing_decision_to_order
from kalshi_predictor.position_sizing.service import (
    ensure_paper_decision_sized,
    position_sizing_decision_id,
)
from kalshi_predictor.signals.attribution import attribute_paper_order_signals
from kalshi_predictor.utils.decimals import ONE_DOLLAR, decimal_to_str, midpoint, to_decimal
from kalshi_predictor.utils.time import utc_now


def create_paper_order(
    session: Session,
    decision: PaperDecision,
    *,
    settings: Settings | None = None,
) -> PaperOrder | None:
    forecast_id = decision.forecast_id
    if settings is not None and settings.learning_mode:
        from kalshi_predictor.learning.duplicates import recent_duplicate_order

        if (
            recent_duplicate_order(
                session,
                ticker=decision.ticker,
                model_name=decision.model_name,
                side=decision.side,
                cooldown_hours=settings.learning_duplicate_cooldown_hours,
            )
            is not None
        ):
            return None
        if forecast_id is not None and get_existing_order_for_forecast(session, forecast_id):
            forecast_id = None
    elif forecast_id is not None and get_existing_order_for_forecast(session, forecast_id):
        return None
    sized_decision = ensure_paper_decision_sized(session, decision, settings=settings)
    if sized_decision.quantity <= 0:
        return None

    order = PaperOrder(
        ticker=sized_decision.ticker,
        forecast_id=forecast_id,
        created_at=utc_now(),
        model_name=sized_decision.model_name,
        side=sized_decision.side,
        probability=decimal_to_str(sized_decision.probability) or "0",
        market_price=decimal_to_str(sized_decision.market_price) or "0",
        limit_price=decimal_to_str(sized_decision.limit_price) or "0",
        edge=decimal_to_str(sized_decision.edge) or "0",
        quantity=sized_decision.quantity,
        status=ORDER_OPEN,
        reason=sized_decision.reason,
        raw_decision_json=encode_json(sized_decision.raw_decision_json),
    )
    session.add(order)
    session.flush()
    sizing_decision_id = position_sizing_decision_id(sized_decision)
    if sizing_decision_id is not None:
        attach_position_sizing_decision_to_order(
            session,
            decision_id=sizing_decision_id,
            order=order,
        )
    risk_decision_id = advanced_risk_decision_id(sized_decision)
    if risk_decision_id is not None:
        attach_advanced_risk_decision_to_order(
            session,
            decision_id=risk_decision_id,
            order=order,
        )
    attribute_paper_order_signals(session, order)
    from kalshi_predictor.memory.capture import capture_paper_order_created

    capture_paper_order_created(session, order, settings=settings)
    return order


def get_open_paper_orders(session: Session) -> list[PaperOrder]:
    return list(
        session.scalars(
            select(PaperOrder)
            .where(PaperOrder.status == ORDER_OPEN)
            .order_by(PaperOrder.created_at, PaperOrder.id)
        )
    )


def mark_order_filled(
    session: Session,
    order: PaperOrder,
    filled_at: datetime | None = None,
) -> None:
    order.status = ORDER_FILLED
    if filled_at is not None:
        raw_decision = _decode_raw_order(order)
        raw_decision["filled_at"] = filled_at.isoformat()
        order.raw_decision_json = encode_json(raw_decision)
    session.add(order)
    mark_reservation_filled_for_order(session, order)


def insert_paper_fill(
    session: Session,
    *,
    order: PaperOrder,
    price: Decimal,
    quantity: int,
    fee: Decimal,
    filled_at: datetime | None = None,
) -> PaperFill:
    resolved_filled_at = filled_at or utc_now()
    fill = PaperFill(
        paper_order_id=order.id,
        ticker=order.ticker,
        filled_at=resolved_filled_at,
        side=order.side,
        price=decimal_to_str(price) or "0",
        quantity=quantity,
        fee=decimal_to_str(fee) or "0",
        raw_fill_json=encode_json(
            {
                "paper_order_id": order.id,
                "ticker": order.ticker,
                "side": order.side,
                "price": decimal_to_str(price),
                "quantity": quantity,
                "fee": decimal_to_str(fee),
                "filled_at": resolved_filled_at.isoformat(),
                "simulation": "immediate_fill_v1",
            }
        ),
    )
    session.add(fill)
    session.flush()
    from kalshi_predictor.memory.capture import capture_paper_fill

    capture_paper_fill(session, fill, settings=None)
    return fill


def get_position(session: Session, ticker: str) -> PaperPosition | None:
    return _pending_position(session, ticker) or session.get(PaperPosition, ticker)


def upsert_position(
    session: Session,
    *,
    ticker: str,
    yes_contracts: int = 0,
    no_contracts: int = 0,
    avg_yes_price: Decimal | None = None,
    avg_no_price: Decimal | None = None,
    realized_pnl: Decimal | None = None,
) -> PaperPosition:
    position = get_position(session, ticker)
    if position is None:
        position = PaperPosition(
            ticker=ticker,
            yes_contracts=0,
            no_contracts=0,
            avg_yes_price=None,
            avg_no_price=None,
            realized_pnl="0",
            updated_at=utc_now(),
        )
        session.add(position)

    position.yes_contracts = yes_contracts
    position.no_contracts = no_contracts
    position.avg_yes_price = decimal_to_str(avg_yes_price)
    position.avg_no_price = decimal_to_str(avg_no_price)
    if realized_pnl is not None:
        position.realized_pnl = decimal_to_str(realized_pnl) or "0"
    position.updated_at = utc_now()
    return position


def update_position_for_fill(session: Session, fill: PaperFill) -> PaperPosition:
    position = get_position(session, fill.ticker)
    if position is None:
        position = PaperPosition(
            ticker=fill.ticker,
            yes_contracts=0,
            no_contracts=0,
            avg_yes_price=None,
            avg_no_price=None,
            realized_pnl="0",
            updated_at=utc_now(),
        )
        session.add(position)

    price = to_decimal(fill.price) or Decimal("0")
    if fill.side == BUY_YES:
        old_contracts = position.yes_contracts
        new_contracts = old_contracts + fill.quantity
        position.avg_yes_price = decimal_to_str(
            _weighted_average(position.avg_yes_price, old_contracts, price, fill.quantity)
        )
        position.yes_contracts = new_contracts
    elif fill.side == BUY_NO:
        old_contracts = position.no_contracts
        new_contracts = old_contracts + fill.quantity
        position.avg_no_price = decimal_to_str(
            _weighted_average(position.avg_no_price, old_contracts, price, fill.quantity)
        )
        position.no_contracts = new_contracts
    else:
        raise ValueError(f"Unsupported paper fill side for Phase 2: {fill.side}")

    position.updated_at = utc_now()
    session.flush()
    from kalshi_predictor.memory.capture import capture_position_opened

    capture_position_opened(session, fill, position)
    return position


def get_latest_forecast_per_ticker(
    session: Session,
    *,
    model_name: str | None = None,
    ticker_scope: set[str] | list[str] | tuple[str, ...] | None = None,
) -> list[Forecast]:
    statement = select(
        Forecast,
        func.row_number()
        .over(
            partition_by=Forecast.ticker,
            order_by=(desc(Forecast.forecasted_at), desc(Forecast.id)),
        )
        .label("row_number"),
    )
    if model_name:
        statement = statement.where(Forecast.model_name == model_name)
    if ticker_scope is not None:
        tickers = sorted({str(ticker) for ticker in ticker_scope if str(ticker).strip()})
        if not tickers:
            return []
        statement = statement.where(Forecast.ticker.in_(tickers))
    ranked = statement.subquery()
    forecast = aliased(Forecast, ranked)
    return list(
        session.scalars(
            select(forecast).where(ranked.c.row_number == 1).order_by(forecast.ticker)
        )
    )


def get_latest_snapshot_for_ticker(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def get_existing_order_for_forecast(session: Session, forecast_id: int) -> PaperOrder | None:
    for item in session.new:
        if isinstance(item, PaperOrder) and item.forecast_id == forecast_id:
            return item
    return session.scalar(
        select(PaperOrder).where(PaperOrder.forecast_id == forecast_id).limit(1)
    )


def get_paper_summary(session: Session) -> PaperSummary:
    total_orders = _count(session, select(func.count()).select_from(PaperOrder))
    filled_orders = _count(
        session,
        select(func.count()).select_from(PaperOrder).where(PaperOrder.status == ORDER_FILLED),
    )
    open_orders = _count(
        session,
        select(func.count()).select_from(PaperOrder).where(PaperOrder.status == ORDER_OPEN),
    )
    positions = list(session.scalars(select(PaperPosition).order_by(PaperPosition.ticker)))
    active_positions = sum(
        1 for position in positions if position.yes_contracts or position.no_contracts
    )
    realized = sum(
        ((to_decimal(position.realized_pnl) or Decimal("0")) for position in positions),
        Decimal("0"),
    )
    unrealized = _estimate_unrealized_pnl(session, positions)
    top_positions = _top_positions(positions)
    recent_fills = _recent_fills(session)
    return PaperSummary(
        total_orders=total_orders,
        filled_orders=filled_orders,
        open_orders=open_orders,
        active_positions=active_positions,
        total_realized_pnl=realized,
        estimated_unrealized_pnl=unrealized,
        total_pnl=realized + unrealized,
        top_positions=top_positions,
        recent_fills=recent_fills,
    )


def reset_paper_data(session: Session) -> None:
    for table in (PaperPnl, PaperFill, PaperOrder, PaperPosition):
        session.execute(delete(table))


def open_order_count(session: Session) -> int:
    return _count(
        session,
        select(func.count()).select_from(PaperOrder).where(PaperOrder.status == ORDER_OPEN),
    )


def total_fees_for_ticker(session: Session, ticker: str) -> Decimal:
    fees = session.scalars(select(PaperFill.fee).where(PaperFill.ticker == ticker)).all()
    return sum(((to_decimal(fee) or Decimal("0")) for fee in fees), Decimal("0"))


def latest_settlement_for_ticker(session: Session, ticker: str) -> Settlement | None:
    return session.get(Settlement, ticker)


def mark_position_realized(
    session: Session,
    position: PaperPosition,
    realized_pnl: Decimal,
) -> None:
    position.realized_pnl = decimal_to_str(realized_pnl) or "0"
    position.updated_at = utc_now()
    session.add(position)


def latest_pnl_rows(session: Session) -> list[PaperPnl]:
    return list(
        session.scalars(
            select(PaperPnl).order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id)).limit(100)
        )
    )


def _weighted_average(
    old_avg_price: str | None,
    old_quantity: int,
    new_price: Decimal,
    new_quantity: int,
) -> Decimal:
    total_quantity = old_quantity + new_quantity
    if total_quantity <= 0:
        return Decimal("0")
    old_price = to_decimal(old_avg_price) or Decimal("0")
    return ((old_price * old_quantity) + (new_price * new_quantity)) / total_quantity


def _pending_position(session: Session, ticker: str) -> PaperPosition | None:
    for item in session.new:
        if isinstance(item, PaperPosition) and item.ticker == ticker:
            return item
    return None


def _count(session: Session, statement: Any) -> int:
    value = session.scalar(statement)
    return int(value or 0)


def _estimate_unrealized_pnl(session: Session, positions: Iterable[PaperPosition]) -> Decimal:
    total = Decimal("0")
    for position in positions:
        settlement = latest_settlement_for_ticker(session, position.ticker)
        if settlement is not None and settlement.result is not None:
            if settlement.result.strip().lower() in {"yes", "no"}:
                continue
        snapshot = get_latest_snapshot_for_ticker(session, position.ticker)
        mark_yes, mark_no = _mark_prices(snapshot)
        yes_cost = (to_decimal(position.avg_yes_price) or Decimal("0")) * position.yes_contracts
        no_cost = (to_decimal(position.avg_no_price) or Decimal("0")) * position.no_contracts
        fees = total_fees_for_ticker(session, position.ticker)
        yes_value = (mark_yes or Decimal("0")) * position.yes_contracts
        no_value = (mark_no or Decimal("0")) * position.no_contracts
        total += yes_value + no_value - yes_cost - no_cost - fees
    return total


def _mark_prices(snapshot: MarketSnapshot | None) -> tuple[Decimal | None, Decimal | None]:
    if snapshot is None:
        return None, None

    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    no_bid = to_decimal(snapshot.best_no_bid)
    no_ask = to_decimal(snapshot.best_no_ask)

    mark_yes = yes_bid
    if mark_yes is None and yes_ask is not None:
        mark_yes = yes_ask
    if yes_bid is not None and yes_ask is not None:
        mark_yes = midpoint(yes_bid, yes_ask)

    mark_no = no_bid
    if mark_no is None and no_ask is not None:
        mark_no = no_ask
    if no_bid is not None and no_ask is not None:
        mark_no = midpoint(no_bid, no_ask)
    if mark_no is None and mark_yes is not None:
        mark_no = ONE_DOLLAR - mark_yes
    if mark_yes is None and mark_no is not None:
        mark_yes = ONE_DOLLAR - mark_no

    return mark_yes, mark_no


def _top_positions(positions: list[PaperPosition]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for position in positions:
        yes_cost = (to_decimal(position.avg_yes_price) or Decimal("0")) * position.yes_contracts
        no_cost = (to_decimal(position.avg_no_price) or Decimal("0")) * position.no_contracts
        exposure = yes_cost + no_cost
        if exposure == 0 and not position.yes_contracts and not position.no_contracts:
            continue
        rows.append(
            {
                "ticker": position.ticker,
                "yes_contracts": position.yes_contracts,
                "no_contracts": position.no_contracts,
                "avg_yes_price": position.avg_yes_price,
                "avg_no_price": position.avg_no_price,
                "realized_pnl": position.realized_pnl,
                "exposure": decimal_to_str(exposure),
            }
        )
    return sorted(
        rows,
        key=lambda row: to_decimal(row["exposure"]) or Decimal("0"),
        reverse=True,
    )[:10]


def _recent_fills(session: Session) -> list[dict[str, Any]]:
    fills = session.scalars(
        select(PaperFill).order_by(desc(PaperFill.filled_at), desc(PaperFill.id)).limit(10)
    )
    return [
        {
            "id": fill.id,
            "ticker": fill.ticker,
            "filled_at": fill.filled_at.isoformat(),
            "side": fill.side,
            "price": fill.price,
            "quantity": fill.quantity,
            "fee": fill.fee,
        }
        for fill in fills
    ]


def _decode_raw_order(order: PaperOrder) -> dict[str, Any]:
    try:
        import json

        decoded = json.loads(order.raw_decision_json)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
