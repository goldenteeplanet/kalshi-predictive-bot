from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.engine import (
    UNKNOWN,
    AdvancedRiskConfig,
    AdvancedRiskEngine,
    AdvancedRiskMode,
    AdvancedRiskRequest,
    MarketRiskSnapshot,
    PortfolioRiskSnapshot,
    TradeEdgeStatistics,
)
from kalshi_predictor.advanced_risk.repository import (
    active_unattached_reservations,
    high_water_equity,
    insert_advanced_risk_decision,
    reservation_lock,
    reserve_advanced_risk,
)
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    Forecast,
    Market,
    MarketSnapshot,
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
)
from kalshi_predictor.position_sizing.sizer import PositionSizingDecision
from kalshi_predictor.tournament.ranking import (
    classify_forecast_category,
    classify_market_category,
    default_category_for_model,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class PaperAdvancedRiskResult:
    request: AdvancedRiskRequest
    decision: Any
    record_id: int
    reservation_id: int | None


def ensure_paper_decision_advanced_risk_checked(
    session: Session,
    decision: PaperDecision,
    *,
    settings: Settings | None = None,
    phase_3m_decision: PositionSizingDecision | None = None,
    decision_timestamp: datetime | None = None,
) -> PaperDecision:
    if _existing_advanced_risk_decision_id(decision.raw_decision_json) is not None:
        return decision

    resolved_settings = settings or get_settings()
    timestamp = decision_timestamp or utc_now()
    with reservation_lock():
        result = apply_advanced_risk_to_paper_decision(
            session,
            decision=decision,
            settings=resolved_settings,
            phase_3m_decision=phase_3m_decision,
            decision_timestamp=timestamp,
        )
    raw_decision = dict(decision.raw_decision_json)
    raw_decision["advanced_risk_decision_id"] = result.record_id
    raw_decision["advanced_risk_decision"] = result.decision.as_dict()
    raw_decision["advanced_risk_request"] = _request_summary(result.request)
    if result.reservation_id is not None:
        raw_decision["advanced_risk_reservation_id"] = result.reservation_id
    raw_decision["quantity"] = result.decision.executed_contracts
    reason = (
        f"{decision.reason} Advanced risk {result.decision.action.value} "
        f"candidate {result.decision.live_candidate_contracts}, executed "
        f"{result.decision.executed_contracts}."
    )
    return replace(
        decision,
        quantity=result.decision.executed_contracts,
        reason=reason,
        raw_decision_json=raw_decision,
    )


def apply_advanced_risk_to_paper_decision(
    session: Session,
    *,
    decision: PaperDecision,
    settings: Settings,
    phase_3m_decision: PositionSizingDecision | None,
    decision_timestamp: datetime,
) -> PaperAdvancedRiskResult:
    config = AdvancedRiskConfig.from_settings(settings)
    request = advanced_risk_request_for_paper_decision(
        session,
        decision=decision,
        settings=settings,
        phase_3m_decision=phase_3m_decision,
        decision_timestamp=decision_timestamp,
    )
    risk_decision = AdvancedRiskEngine(config).decide(request)
    record = insert_advanced_risk_decision(
        session,
        risk_decision,
        request,
        ticker=decision.ticker,
        position_sizing_decision_id=_position_sizing_decision_id(decision.raw_decision_json),
        raw={
            "paper_side": decision.side,
            "paper_price": decimal_to_str(decision.limit_price) or str(decision.limit_price),
            "paper_edge": decimal_to_str(decision.edge) or str(decision.edge),
        },
    )
    reservation_id = None
    if config.mode == AdvancedRiskMode.LIVE and risk_decision.executed_contracts > 0:
        reservation = reserve_advanced_risk(
            session,
            decision_record=record,
            decision=risk_decision,
            request=request,
            ticker=decision.ticker,
        )
        reservation_id = reservation.id if reservation is not None else None
        risk_decision = risk_decision.with_reservation(reservation_id)
    return PaperAdvancedRiskResult(
        request=request,
        decision=risk_decision,
        record_id=int(record.id),
        reservation_id=reservation_id,
    )


def advanced_risk_request_for_paper_decision(
    session: Session,
    *,
    decision: PaperDecision,
    settings: Settings,
    phase_3m_decision: PositionSizingDecision | None,
    decision_timestamp: datetime,
) -> AdvancedRiskRequest:
    timestamp = _ensure_utc(decision_timestamp)
    forecast = session.get(Forecast, decision.forecast_id) if decision.forecast_id else None
    market = session.get(Market, decision.ticker)
    market_snapshot = _latest_snapshot_for_ticker(session, decision.ticker)
    category = _category_for_decision(session, forecast=forecast, market=market, decision=decision)
    model_id = str(decision.model_name or (forecast.model_name if forecast else "") or UNKNOWN)
    phase_tier, phase_contracts, confidence = _phase_3m_boundary(
        decision=decision,
        phase_3m_decision=phase_3m_decision,
    )
    portfolio = _portfolio_snapshot(
        session,
        settings=settings,
        decision_timestamp=timestamp,
    )
    market_risk = _market_snapshot(
        market_snapshot,
        side=decision.side,
        decision_timestamp=timestamp,
    )
    edge_stats = _edge_statistics(
        session,
        forecast=forecast,
        model_id=model_id,
        category_id=category,
        decision_timestamp=timestamp,
    )
    return AdvancedRiskRequest(
        version=settings.advanced_risk_engine_version,
        decision_timestamp=timestamp,
        trade_intent_id=_trade_intent_id(decision),
        order_correlation_id=_trade_intent_id(decision),
        strategy_id=str(decision.raw_decision_json.get("strategy") or "paper_edge_v1"),
        model_id=model_id,
        category_id=category or UNKNOWN,
        instrument_id=decision.ticker,
        correlation_group_id=category if category and category != UNKNOWN else None,
        direction="LONG",
        phase_3m_tier=phase_tier,
        phase_3m_proposed_contracts=phase_contracts,
        confidence_score=confidence,
        entry_price=decision.limit_price,
        stop_price=Decimal("0"),
        point_value=Decimal("1"),
        tick_size=Decimal("0.01"),
        estimated_round_trip_fees=settings.paper_default_fee_per_contract,
        estimated_slippage_per_contract=settings.advanced_risk_estimated_slippage_per_contract,
        gap_or_tail_buffer_per_contract=settings.advanced_risk_gap_tail_buffer_per_contract,
        portfolio_snapshot=portfolio,
        market_snapshot=market_risk,
        edge_statistics=edge_stats,
        external_hard_risk_block=decision.quantity <= 0,
        external_margin_cap=None,
        external_buying_power_cap=None,
    )


def advanced_risk_decision_id(decision: PaperDecision) -> int | None:
    return _existing_advanced_risk_decision_id(decision.raw_decision_json)


def _portfolio_snapshot(
    session: Session,
    *,
    settings: Settings,
    decision_timestamp: datetime,
) -> PortfolioRiskSnapshot:
    positions = list(session.scalars(select(PaperPosition).order_by(PaperPosition.ticker)))
    open_orders = list(
        session.scalars(
            select(PaperOrder)
            .where(PaperOrder.status == ORDER_OPEN)
            .order_by(PaperOrder.created_at, PaperOrder.id)
        )
    )
    open_category: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    open_model: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    open_instrument: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    pending_category: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    pending_model: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    pending_instrument: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    total_open_risk = Decimal("0")
    total_pending_risk = Decimal("0")
    for position in positions:
        risk = _position_open_risk(position)
        if risk <= 0:
            continue
        category, model = _position_bucket(session, position.ticker)
        open_category[category] += risk
        open_model[model] += risk
        open_instrument[position.ticker] += risk
        total_open_risk += risk
    for order in open_orders:
        risk = _order_risk(order)
        if risk <= 0:
            continue
        category = _order_category(session, order)
        pending_category[category] += risk
        pending_model[str(order.model_name or UNKNOWN)] += risk
        pending_instrument[order.ticker] += risk
        total_pending_risk += risk
    for reservation in active_unattached_reservations(session):
        risk = to_decimal(reservation.reserved_risk) or Decimal("0")
        if risk <= 0:
            continue
        pending_category[str(reservation.category_id or UNKNOWN)] += risk
        pending_model[str(reservation.model_id or UNKNOWN)] += risk
        pending_instrument[str(reservation.instrument_id or reservation.ticker)] += risk
        total_pending_risk += risk
    realized, unrealized = _session_pnl(session, decision_timestamp=decision_timestamp)
    account_equity = settings.advanced_risk_default_account_equity + realized + unrealized
    high_water = high_water_equity(session, account_key="paper", observed_equity=account_equity)
    version = (
        f"paper:{len(positions)}:{len(open_orders)}:"
        f"{sum(1 for _ in active_unattached_reservations(session))}"
    )
    return PortfolioRiskSnapshot(
        snapshot_id=f"paper-{decision_timestamp.isoformat()}",
        snapshot_version=version,
        captured_at=decision_timestamp,
        account_equity=account_equity,
        start_of_session_equity=settings.advanced_risk_default_account_equity,
        high_water_equity=high_water,
        realized_pnl_session=realized,
        unrealized_pnl_session=unrealized,
        current_total_open_risk=total_open_risk,
        current_pending_reserved_risk=total_pending_risk,
        category_open_risk=dict(open_category),
        category_pending_reserved_risk=dict(pending_category),
        model_open_risk=dict(open_model),
        model_pending_reserved_risk=dict(pending_model),
        instrument_open_risk=dict(open_instrument),
        instrument_pending_reserved_risk=dict(pending_instrument),
        correlation_group_open_risk=dict(open_category),
        correlation_group_pending_reserved_risk=dict(pending_category),
        existing_position_contracts=sum(p.yes_contracts + p.no_contracts for p in positions),
        existing_pending_entry_contracts=sum(order.quantity for order in open_orders),
    )


def _market_snapshot(
    snapshot: MarketSnapshot | None,
    *,
    side: str,
    decision_timestamp: datetime,
) -> MarketRiskSnapshot:
    if snapshot is None:
        return MarketRiskSnapshot(
            captured_at=decision_timestamp,
            bid_price=None,
            ask_price=None,
            quote_age_ms=None,
            market_status="UNKNOWN",
            data_quality_status="INVALID",
        )
    bid, ask = _bid_ask_for_side(snapshot, side)
    raw_market = decode_json(snapshot.raw_market_json)
    raw_book = decode_json(snapshot.raw_orderbook_json)
    captured_at = _ensure_utc(snapshot.captured_at)
    quote_age_ms = int((decision_timestamp - captured_at).total_seconds() * 1000)
    depth = _executable_depth(raw_book, side=side)
    return MarketRiskSnapshot(
        captured_at=captured_at,
        bid_price=bid,
        ask_price=ask,
        last_price=to_decimal(snapshot.last_price_dollars),
        quote_age_ms=max(0, quote_age_ms),
        executable_depth_contracts=depth,
        depth_price_band_ticks=Decimal("5") if depth is not None else None,
        recent_volume_contracts=(
            to_decimal(snapshot.volume_24h_fp)
            or to_decimal(snapshot.volume_fp)
            or to_decimal(raw_market.get("volume"))
        ),
        recent_volume_window_seconds=86400,
        average_daily_volume_contracts=to_decimal(snapshot.volume_24h_fp),
        open_interest_contracts=to_decimal(snapshot.open_interest_fp),
        expected_market_impact_per_contract=to_decimal(
            raw_market.get("expected_market_impact_per_contract")
        ),
        expected_slippage_per_contract=to_decimal(raw_market.get("expected_slippage_per_contract")),
        market_status=str(snapshot.status or raw_market.get("status") or "UNKNOWN").upper(),
        data_quality_status="VALID" if bid is not None and ask is not None else "INVALID",
    )


def _edge_statistics(
    session: Session,
    *,
    forecast: Forecast | None,
    model_id: str,
    category_id: str,
    decision_timestamp: datetime,
) -> TradeEdgeStatistics | None:
    rows = _closed_trade_rows(session, decision_timestamp=decision_timestamp)
    ticker = forecast.ticker if forecast is not None else None
    model_ticker_rows = [
        row for row in rows if row["model_id"] == model_id and row["ticker"] == ticker
    ]
    buckets = (
        ("model+ticker", model_ticker_rows),
        (
            "model+category",
            [row for row in rows if row["model_id"] == model_id and row["category"] == category_id],
        ),
        ("model", [row for row in rows if row["model_id"] == model_id]),
        ("category", [row for row in rows if row["category"] == category_id]),
        ("global", rows),
    )
    for bucket_level, bucket_rows in buckets:
        if bucket_rows:
            wins = [row for row in bucket_rows if row["won"]]
            losses = [row for row in bucket_rows if not row["won"]]
            avg_win = _average([row["gross_win"] for row in wins])
            avg_loss = _average([row["gross_loss"] for row in losses])
            if avg_win is None:
                avg_win = Decimal("0.50")
            if avg_loss is None:
                avg_loss = Decimal("0.50")
            latest_closed = max(row["closed_at"] for row in bucket_rows)
            return TradeEdgeStatistics(
                bucket_key=f"{bucket_level}:{model_id}:{category_id}:{ticker or '*'}",
                bucket_level=bucket_level,
                sample_size=len(bucket_rows),
                raw_win_probability=Decimal(len(wins)) / Decimal(len(bucket_rows)),
                average_gross_win_per_contract=avg_win,
                average_gross_loss_per_contract=avg_loss,
                statistics_as_of=latest_closed,
                outcome_basis="GROSS",
            )
    return None


def _closed_trade_rows(
    session: Session,
    *,
    decision_timestamp: datetime,
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(PaperOrder, Forecast, Settlement)
        .join(Forecast, PaperOrder.forecast_id == Forecast.id, isouter=True)
        .join(Settlement, PaperOrder.ticker == Settlement.ticker)
        .where(PaperOrder.status == ORDER_FILLED)
        .where(Settlement.settled_at.is_not(None))
        .where(Settlement.settled_at < decision_timestamp)
        .order_by(Settlement.settled_at, PaperOrder.created_at, PaperOrder.id)
    ).all()
    output: list[dict[str, Any]] = []
    for order, forecast, settlement in rows:
        if settlement.result not in {"yes", "no"}:
            continue
        price = to_decimal(order.limit_price) or to_decimal(order.market_price) or Decimal("0")
        won = _order_won(order, settlement)
        category = (
            classify_forecast_category(session, forecast)
            if forecast is not None
            else _order_category(session, order)
        )
        output.append(
            {
                "ticker": order.ticker,
                "model_id": order.model_name,
                "category": category,
                "won": won,
                "gross_win": max(Decimal("0"), Decimal("1") - price),
                "gross_loss": max(Decimal("0"), price),
                "closed_at": _ensure_utc(settlement.settled_at),
            }
        )
    return output


def _phase_3m_boundary(
    *,
    decision: PaperDecision,
    phase_3m_decision: PositionSizingDecision | None,
) -> tuple[str, int, Decimal | None]:
    raw_phase = decision.raw_decision_json.get("position_sizing_decision")
    quantity = int(decision.quantity)
    tier = {1: "LOW", 3: "MEDIUM", 5: "HIGH"}.get(quantity, "LOW")
    confidence: Decimal | None = None
    if phase_3m_decision is not None:
        confidence = to_decimal(phase_3m_decision.factor_scores.get("confidence"))
    if confidence is None and isinstance(raw_phase, dict):
        factor_scores = raw_phase.get("factor_scores") or {}
        confidence = to_decimal(factor_scores.get("confidence"))
    if confidence is None:
        confidence = _decision_confidence(decision)
    return tier, quantity, confidence


def _decision_confidence(decision: PaperDecision) -> Decimal | None:
    probability = to_decimal(decision.probability)
    if probability is None:
        return None
    if decision.side == BUY_NO:
        probability = Decimal("1") - probability
    return max(Decimal("0"), min(Decimal("1"), probability))


def _category_for_decision(
    session: Session,
    *,
    forecast: Forecast | None,
    market: Market | None,
    decision: PaperDecision,
) -> str:
    if forecast is not None:
        return classify_forecast_category(session, forecast)
    if market is not None:
        text = " ".join(str(item or "") for item in (market.title, market.subtitle, market.ticker))
        category = classify_market_category(text)
        if category != "unknown":
            return category
    if decision.model_name:
        return default_category_for_model(decision.model_name)
    return UNKNOWN


def _position_bucket(session: Session, ticker: str) -> tuple[str, str]:
    order = session.scalar(
        select(PaperOrder)
        .where(PaperOrder.ticker == ticker)
        .order_by(desc(PaperOrder.created_at), desc(PaperOrder.id))
        .limit(1)
    )
    if order is not None:
        return _order_category(session, order), str(order.model_name or UNKNOWN)
    market = session.get(Market, ticker)
    if market is not None:
        category = classify_market_category(
            " ".join(str(item or "") for item in (market.title, market.subtitle, ticker))
        )
        return category if category != "unknown" else UNKNOWN, UNKNOWN
    return UNKNOWN, UNKNOWN


def _order_category(session: Session, order: PaperOrder) -> str:
    if order.forecast_id is not None:
        forecast = session.get(Forecast, order.forecast_id)
        if forecast is not None:
            return classify_forecast_category(session, forecast)
    market = session.get(Market, order.ticker)
    if market is not None:
        category = classify_market_category(
            " ".join(str(item or "") for item in (market.title, market.subtitle, order.ticker))
        )
        if category != "unknown":
            return category
    return default_category_for_model(order.model_name)


def _position_open_risk(position: PaperPosition) -> Decimal:
    yes = Decimal(position.yes_contracts) * (to_decimal(position.avg_yes_price) or Decimal("0"))
    no = Decimal(position.no_contracts) * (to_decimal(position.avg_no_price) or Decimal("0"))
    return yes + no


def _order_risk(order: PaperOrder) -> Decimal:
    price = to_decimal(order.limit_price) or to_decimal(order.market_price) or Decimal("0")
    return price * Decimal(order.quantity)


def _session_pnl(
    session: Session,
    *,
    decision_timestamp: datetime,
) -> tuple[Decimal, Decimal]:
    start = _session_start(decision_timestamp)
    rows = list(session.scalars(select(PaperPnl).where(PaperPnl.calculated_at >= start)))
    if not rows:
        return Decimal("0"), Decimal("0")
    realized = sum((to_decimal(row.realized_pnl) or Decimal("0")) for row in rows)
    unrealized = sum((to_decimal(row.unrealized_pnl) or Decimal("0")) for row in rows)
    return realized, unrealized


def _session_start(now: datetime) -> datetime:
    current = _ensure_utc(now)
    reset = time(hour=0, minute=0, tzinfo=UTC)
    start = datetime.combine(current.date(), reset, tzinfo=UTC)
    if current < start:
        start = start - timedelta(days=1)
    return start


def _bid_ask_for_side(snapshot: MarketSnapshot, side: str) -> tuple[Decimal | None, Decimal | None]:
    if side == BUY_NO:
        return to_decimal(snapshot.best_no_bid), to_decimal(snapshot.best_no_ask)
    return to_decimal(snapshot.best_yes_bid), to_decimal(snapshot.best_yes_ask)


def _executable_depth(raw_orderbook: dict[str, Any], *, side: str) -> Decimal | None:
    orderbook = raw_orderbook.get("orderbook_fp") if raw_orderbook else None
    if not isinstance(orderbook, dict):
        return None
    key = "no_dollars" if side == BUY_NO else "yes_dollars"
    levels = orderbook.get(key)
    if not isinstance(levels, list):
        return None
    depth = Decimal("0")
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        quantity = to_decimal(level[1])
        if quantity is not None:
            depth += quantity
    return depth


def _latest_snapshot_for_ticker(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _order_won(order: PaperOrder, settlement: Settlement) -> bool:
    if order.side == BUY_YES:
        return settlement.result == "yes"
    if order.side == BUY_NO:
        return settlement.result == "no"
    return False


def _average(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _trade_intent_id(decision: PaperDecision) -> str:
    if decision.forecast_id is not None:
        return f"forecast:{decision.forecast_id}"
    return f"{decision.ticker}:{decision.model_name}:{decision.side}"


def _position_sizing_decision_id(raw_decision_json: dict[str, Any]) -> int | None:
    value = raw_decision_json.get("position_sizing_decision_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _existing_advanced_risk_decision_id(raw_decision_json: dict[str, Any]) -> int | None:
    value = raw_decision_json.get("advanced_risk_decision_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _request_summary(request: AdvancedRiskRequest) -> dict[str, Any]:
    return {
        "version": request.version,
        "trade_intent_id": request.trade_intent_id,
        "strategy_id": request.strategy_id,
        "model_id": request.model_id,
        "category_id": request.category_id,
        "instrument_id": request.instrument_id,
        "phase_3m_tier": request.phase_3m_tier,
        "phase_3m_proposed_contracts": request.phase_3m_proposed_contracts,
        "confidence_score": decimal_to_str(request.confidence_score),
        "entry_price": decimal_to_str(request.entry_price),
        "portfolio_snapshot_version": request.portfolio_snapshot.snapshot_version,
        "market_snapshot_timestamp": request.market_snapshot.captured_at.isoformat(),
        "edge_bucket": (
            request.edge_statistics.bucket_level if request.edge_statistics else "missing"
        ),
    }
