from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import MarketSnapshot, PaperPnl, PaperPosition
from kalshi_predictor.paper.ledger import (
    get_latest_snapshot_for_ticker,
    latest_settlement_for_ticker,
    mark_position_realized,
    total_fees_for_ticker,
)
from kalshi_predictor.paper.models import PnlSummary
from kalshi_predictor.signals.scoring import refresh_signal_performance
from kalshi_predictor.utils.decimals import ONE_DOLLAR, decimal_to_str, midpoint, to_decimal
from kalshi_predictor.utils.time import utc_now


def calculate_unrealized_pnl(
    position: PaperPosition,
    snapshot: MarketSnapshot | None,
    *,
    fees: Decimal = Decimal("0"),
) -> Decimal:
    mark_yes, mark_no = _mark_prices(snapshot)
    yes_cost = (to_decimal(position.avg_yes_price) or Decimal("0")) * position.yes_contracts
    no_cost = (to_decimal(position.avg_no_price) or Decimal("0")) * position.no_contracts
    yes_value = (mark_yes or Decimal("0")) * position.yes_contracts
    no_value = (mark_no or Decimal("0")) * position.no_contracts
    return yes_value + no_value - yes_cost - no_cost - fees


def calculate_settled_pnl(
    position: PaperPosition,
    settlement_result: str | None,
    *,
    fees: Decimal = Decimal("0"),
) -> Decimal | None:
    if settlement_result is None:
        return None
    normalized = settlement_result.strip().lower()
    if normalized not in {"yes", "no"}:
        return None

    yes_cost = (to_decimal(position.avg_yes_price) or Decimal("0")) * position.yes_contracts
    no_cost = (to_decimal(position.avg_no_price) or Decimal("0")) * position.no_contracts
    total_cost = yes_cost + no_cost + fees
    if normalized == "yes":
        payout = ONE_DOLLAR * position.yes_contracts
    else:
        payout = ONE_DOLLAR * position.no_contracts
    return payout - total_cost


def calculate_settled_pnl_from_yes_value(
    position: PaperPosition,
    yes_settlement_value: Decimal | None,
    *,
    fees: Decimal = Decimal("0"),
) -> Decimal | None:
    if yes_settlement_value is None:
        return None
    if yes_settlement_value < Decimal("0") or yes_settlement_value > ONE_DOLLAR:
        return None

    yes_cost = (to_decimal(position.avg_yes_price) or Decimal("0")) * position.yes_contracts
    no_cost = (to_decimal(position.avg_no_price) or Decimal("0")) * position.no_contracts
    total_cost = yes_cost + no_cost + fees
    payout = (yes_settlement_value * position.yes_contracts) + (
        (ONE_DOLLAR - yes_settlement_value) * position.no_contracts
    )
    return payout - total_cost


def calculate_and_store_pnl(session: Session) -> PnlSummary:
    positions = list(session.scalars(select(PaperPosition).order_by(PaperPosition.ticker)))
    rows_inserted = 0
    total_realized = Decimal("0")
    total_unrealized = Decimal("0")
    now = utc_now()

    for position in positions:
        fees = total_fees_for_ticker(session, position.ticker)
        settlement = latest_settlement_for_ticker(session, position.ticker)
        settlement_result = _settlement_result_for_pnl(settlement)
        yes_settlement_value = _yes_settlement_value_for_pnl(settlement)
        realized = calculate_settled_pnl_from_yes_value(
            position,
            yes_settlement_value,
            fees=fees,
        )
        if realized is None:
            realized = calculate_settled_pnl(position, settlement_result, fees=fees)
        notes = "open market mark-to-market estimate"
        if realized is not None:
            unrealized = Decimal("0")
            mark_position_realized(session, position, realized)
            notes = "settled market realized paper P&L"
        else:
            realized = to_decimal(position.realized_pnl) or Decimal("0")
            snapshot = get_latest_snapshot_for_ticker(session, position.ticker)
            unrealized = calculate_unrealized_pnl(position, snapshot, fees=fees)

        total = realized + unrealized
        pnl_row = PaperPnl(
            ticker=position.ticker,
            calculated_at=now,
            yes_contracts=position.yes_contracts,
            no_contracts=position.no_contracts,
            avg_yes_price=position.avg_yes_price,
            avg_no_price=position.avg_no_price,
            settlement_result=settlement_result,
            realized_pnl=decimal_to_str(realized) or "0",
            unrealized_pnl=decimal_to_str(unrealized) or "0",
            total_pnl=decimal_to_str(total) or "0",
            notes=notes,
        )
        session.add(pnl_row)
        rows_inserted += 1
        total_realized += realized
        total_unrealized += unrealized

    refresh_signal_performance(session)

    return PnlSummary(
        positions_evaluated=len(positions),
        pnl_rows_inserted=rows_inserted,
        realized_pnl=total_realized,
        unrealized_pnl=total_unrealized,
        total_pnl=total_realized + total_unrealized,
    )


def _settlement_result_for_pnl(settlement: object | None) -> str | None:
    if settlement is None:
        return None
    result = getattr(settlement, "result", None)
    yes_value = _yes_settlement_value_for_pnl(settlement)
    if yes_value == ONE_DOLLAR:
        return "yes"
    if yes_value == Decimal("0"):
        return "no"
    if yes_value is not None:
        return _normalize_binary_result(result) or "scalar"
    if result is not None and str(result).strip():
        return str(result)
    return None


def _yes_settlement_value_for_pnl(settlement: object | None) -> Decimal | None:
    if settlement is None:
        return None
    yes_value = to_decimal(getattr(settlement, "yes_settlement_value", None))
    if yes_value is not None:
        return yes_value
    normalized = _normalize_binary_result(getattr(settlement, "result", None))
    if normalized == "yes":
        return ONE_DOLLAR
    if normalized == "no":
        return Decimal("0")
    return None


def _normalize_binary_result(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return "yes"
    if normalized in {"no", "n", "0", "false"}:
        return "no"
    return None


def _mark_prices(snapshot: MarketSnapshot | None) -> tuple[Decimal | None, Decimal | None]:
    if snapshot is None:
        return None, None

    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    no_bid = to_decimal(snapshot.best_no_bid)
    no_ask = to_decimal(snapshot.best_no_ask)

    mark_yes = yes_bid
    if yes_bid is not None and yes_ask is not None:
        mark_yes = midpoint(yes_bid, yes_ask)
    elif mark_yes is None:
        mark_yes = yes_ask

    mark_no = no_bid
    if no_bid is not None and no_ask is not None:
        mark_no = midpoint(no_bid, no_ask)
    elif mark_no is None:
        mark_no = no_ask

    if mark_no is None and mark_yes is not None:
        mark_no = ONE_DOLLAR - mark_yes
    if mark_yes is None and mark_no is not None:
        mark_yes = ONE_DOLLAR - mark_no
    return mark_yes, mark_no
