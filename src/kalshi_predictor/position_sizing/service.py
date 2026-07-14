from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.service import (
    ensure_paper_decision_advanced_risk_checked,
)
from kalshi_predictor.autopilot.repository import current_daily_pnl
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    Forecast,
    MarketSnapshot,
    PaperOrder,
    PaperPosition,
    Settlement,
)
from kalshi_predictor.opportunities.payout_scoring import calculate_payout_metrics
from kalshi_predictor.opportunities.scoring import (
    score_liquidity,
    score_model_confidence,
    score_spread,
    score_time_to_close,
)
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, PaperDecision
from kalshi_predictor.position_sizing.repository import insert_position_sizing_decision
from kalshi_predictor.position_sizing.sizer import (
    DynamicPositionSizer,
    PositionSizingDecision,
    PositionSizingInput,
)
from kalshi_predictor.tournament.ranking import classify_forecast_category
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now


@dataclass(frozen=True)
class PaperSizingResult:
    decision: PositionSizingDecision
    record_id: int
    evidence: dict[str, Any]


def ensure_paper_decision_sized(
    session: Session,
    decision: PaperDecision,
    *,
    settings: Settings | None = None,
) -> PaperDecision:
    resolved_settings = settings or get_settings()
    if _existing_sizing_decision_id(decision.raw_decision_json) is not None:
        return ensure_paper_decision_advanced_risk_checked(
            session,
            decision,
            settings=resolved_settings,
        )

    sizing_result = size_paper_decision(
        session,
        decision=decision,
        settings=resolved_settings,
    )
    raw_decision = dict(decision.raw_decision_json)
    raw_decision["position_sizing_decision_id"] = sizing_result.record_id
    raw_decision["position_sizing_decision"] = sizing_result.decision.as_dict()
    raw_decision["position_sizing_evidence"] = sizing_result.evidence
    raw_decision["quantity"] = sizing_result.decision.executed_contracts
    reason = (
        f"{decision.reason} "
        f"Position sizing {sizing_result.decision.tier.value.upper()} proposed "
        f"{sizing_result.decision.proposed_contracts}, live candidate "
        f"{sizing_result.decision.live_candidate_contracts}, executed "
        f"{sizing_result.decision.executed_contracts}."
    )
    sized_decision = replace(
        decision,
        quantity=sizing_result.decision.executed_contracts,
        reason=reason,
        raw_decision_json=raw_decision,
    )
    return ensure_paper_decision_advanced_risk_checked(
        session,
        sized_decision,
        settings=resolved_settings,
        phase_3m_decision=sizing_result.decision,
    )


def size_paper_decision(
    session: Session,
    *,
    decision: PaperDecision,
    settings: Settings,
    decision_timestamp: datetime | None = None,
) -> PaperSizingResult:
    timestamp = decision_timestamp or utc_now()
    forecast = session.get(Forecast, decision.forecast_id) if decision.forecast_id else None
    snapshot = (
        _snapshot_for_forecast(session, forecast)
        if forecast is not None
        else _latest_snapshot_for_ticker(session, decision.ticker)
    )
    portfolio_cap = _remaining_position_cap(
        session,
        ticker=decision.ticker,
        side=decision.side,
        settings=settings,
    )
    sizing_input, evidence = _input_for_decision(
        session,
        forecast=forecast,
        snapshot=snapshot,
        decision=decision,
        settings=settings,
        portfolio_cap=portfolio_cap,
        decision_timestamp=timestamp,
    )
    sizing_decision = DynamicPositionSizer.from_settings(settings).decide(sizing_input)
    record = insert_position_sizing_decision(
        session,
        sizing_decision,
        ticker=decision.ticker,
        model_name=decision.model_name,
        strategy_id=str(decision.raw_decision_json.get("strategy") or "paper_edge_v1"),
        instrument=decision.ticker,
        trade_intent_id=_trade_intent_id(decision),
        order_correlation_id=_trade_intent_id(decision),
        raw={
            "side": decision.side,
            "price": decimal_to_str(decision.limit_price) or str(decision.limit_price),
            "edge": decimal_to_str(decision.edge) or str(decision.edge),
            "evidence": evidence,
        },
    )
    return PaperSizingResult(
        decision=sizing_decision,
        record_id=int(record.id),
        evidence=evidence,
    )


def position_sizing_decision_id(decision: PaperDecision) -> int | None:
    return _existing_sizing_decision_id(decision.raw_decision_json)


def _input_for_decision(
    session: Session,
    *,
    forecast: Forecast | None,
    snapshot: MarketSnapshot | None,
    decision: PaperDecision,
    settings: Settings,
    portfolio_cap: int,
    decision_timestamp: datetime,
) -> tuple[PositionSizingInput, dict[str, Any]]:
    scores, evidence = _sizing_scores(
        forecast=forecast,
        snapshot=snapshot,
        side=decision.side,
        price=decision.limit_price,
        edge=decision.edge,
        settings=settings,
    )
    drawdown_current, drawdown_max, drawdown_evidence = _drawdown_inputs(
        session,
        settings=settings,
        decision_timestamp=decision_timestamp,
    )
    historical_accuracy, historical_sample_size, history_evidence = _historical_accuracy(
        session,
        forecast=forecast,
        decision_timestamp=decision_timestamp,
    )
    configured_portfolio_cap = settings.dynamic_position_sizing_portfolio_cap
    resolved_portfolio_cap = (
        min(portfolio_cap, configured_portfolio_cap)
        if configured_portfolio_cap is not None
        else portfolio_cap
    )
    evidence = {
        **evidence,
        "drawdown": drawdown_evidence,
        "history": history_evidence,
        "caps": {
            "external_risk_cap": settings.dynamic_position_sizing_external_risk_cap,
            "margin_cap": settings.dynamic_position_sizing_margin_cap,
            "portfolio_cap": resolved_portfolio_cap,
            "position_remaining_cap": portfolio_cap,
        },
    }
    return (
        PositionSizingInput(
            confidence_score=scores["confidence_score"],
            opportunity_score=scores["opportunity_score"],
            liquidity_score=scores["liquidity_score"],
            current_drawdown_fraction=drawdown_current,
            max_drawdown_fraction=drawdown_max,
            historical_accuracy=historical_accuracy,
            historical_sample_size=historical_sample_size,
            external_risk_cap=settings.dynamic_position_sizing_external_risk_cap,
            margin_cap=settings.dynamic_position_sizing_margin_cap,
            portfolio_cap=resolved_portfolio_cap,
            hard_risk_block=portfolio_cap <= 0,
            decision_timestamp=decision_timestamp,
        ),
        evidence,
    )


def _sizing_scores(
    *,
    forecast: Forecast | None,
    snapshot: MarketSnapshot | None,
    side: str,
    price: Decimal,
    edge: Decimal,
    settings: Settings,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    if forecast is None or snapshot is None:
        return (
            {
                "confidence_score": None,
                "opportunity_score": None,
                "liquidity_score": None,
            },
            {
                "scores_available": False,
                "missing": [
                    "forecast" if forecast is None else "",
                    "market_snapshot" if snapshot is None else "",
                ],
            },
        )

    raw_market = decode_json(snapshot.raw_market_json)
    yes_probability = to_decimal(forecast.yes_probability)
    spread = _spread(snapshot)
    time_to_close = _time_to_close_minutes(snapshot, raw_market)
    liquidity_evidence = raw_market.get("liquidity_dollars")
    liquidity_score = score_liquidity(
        volume=snapshot.volume_fp,
        open_interest=snapshot.open_interest_fp,
        liquidity=liquidity_evidence,
    )
    spread_score = score_spread(spread, max_spread=settings.opportunity_max_spread)
    time_score = score_time_to_close(
        time_to_close,
        min_minutes=settings.opportunity_min_time_to_close_minutes,
    )
    model_confidence_score = score_model_confidence(yes_probability)
    payout_metrics = calculate_payout_metrics(
        side=side,
        yes_probability=yes_probability,
        cost=price,
        edge=edge,
        liquidity_score=liquidity_score,
        spread_score=spread_score,
        confidence_score=model_confidence_score,
        time_score=time_score,
    )
    opportunity_score = payout_metrics.payout_adjusted_score
    evidence = {
        "scores_available": True,
        "source": "opportunity_scoring_v1",
        "confidence_score_0_100": decimal_to_str(model_confidence_score),
        "opportunity_score_0_100": decimal_to_str(opportunity_score),
        "liquidity_score_0_100": decimal_to_str(liquidity_score),
        "liquidity_evidence": {
            "volume": snapshot.volume_fp,
            "open_interest": snapshot.open_interest_fp,
            "liquidity": liquidity_evidence,
            "spread": decimal_to_str(spread),
            "time_to_close_minutes": decimal_to_str(time_to_close),
        },
        "payout_metrics": payout_metrics.as_dict(),
    }
    return (
        {
            "confidence_score": _score_0_100_to_unit(model_confidence_score),
            "opportunity_score": _score_0_100_to_unit(opportunity_score),
            "liquidity_score": _score_0_100_to_unit(liquidity_score),
        },
        evidence,
    )


def _historical_accuracy(
    session: Session,
    *,
    forecast: Forecast | None,
    decision_timestamp: datetime,
) -> tuple[float, int, dict[str, Any]]:
    if forecast is None:
        return 0.5, 0, {"bucket": "prior", "reason": "missing_forecast"}
    rows = _closed_historical_orders(session, decision_timestamp=decision_timestamp)
    target_category = classify_forecast_category(session, forecast)
    buckets = (
        (
            "model+ticker",
            [
                row
                for row in rows
                if row["model_name"] == forecast.model_name and row["ticker"] == forecast.ticker
            ],
        ),
        (
            "model+category",
            [
                row
                for row in rows
                if row["model_name"] == forecast.model_name and row["category"] == target_category
            ],
        ),
        (
            "model",
            [row for row in rows if row["model_name"] == forecast.model_name],
        ),
        ("global", rows),
    )
    for bucket_name, bucket_rows in buckets:
        if bucket_rows:
            wins = sum(1 for row in bucket_rows if row["won"])
            sample_size = len(bucket_rows)
            return (
                wins / sample_size,
                sample_size,
                {
                    "bucket": bucket_name,
                    "target_category": target_category,
                    "wins": wins,
                    "sample_size": sample_size,
                    "lookahead_filter": "settlement.settled_at < decision_timestamp",
                },
            )
    return (
        0.5,
        0,
        {
            "bucket": "prior",
            "target_category": target_category,
            "sample_size": 0,
            "lookahead_filter": "settlement.settled_at < decision_timestamp",
        },
    )


def _closed_historical_orders(
    session: Session,
    *,
    decision_timestamp: datetime,
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(PaperOrder, Forecast, Settlement)
        .join(Forecast, PaperOrder.forecast_id == Forecast.id, isouter=True)
        .join(Settlement, PaperOrder.ticker == Settlement.ticker)
        .where(Settlement.settled_at.is_not(None))
        .where(Settlement.settled_at < decision_timestamp)
        .order_by(Settlement.settled_at, PaperOrder.created_at, PaperOrder.id)
    ).all()
    output: list[dict[str, Any]] = []
    category_cache: dict[int, str] = {}
    for order, forecast, settlement in rows:
        if forecast is None or settlement.result not in {"yes", "no"}:
            continue
        forecast_id = int(forecast.id or 0)
        if forecast_id not in category_cache:
            category_cache[forecast_id] = classify_forecast_category(session, forecast)
        output.append(
            {
                "ticker": order.ticker,
                "model_name": order.model_name,
                "category": category_cache[forecast_id],
                "won": _order_won(order, settlement),
                "closed_at": settlement.settled_at,
            }
        )
    return output


def _order_won(order: PaperOrder, settlement: Settlement) -> bool:
    if order.side == BUY_YES:
        return settlement.result == "yes"
    if order.side == BUY_NO:
        return settlement.result == "no"
    return False


def _drawdown_inputs(
    session: Session,
    *,
    settings: Settings,
    decision_timestamp: datetime,
) -> tuple[float | None, float | None, dict[str, Any]]:
    max_daily_drawdown = settings.autopilot_max_daily_drawdown
    if max_daily_drawdown <= 0:
        return None, None, {"source": "autopilot_max_daily_drawdown", "missing": True}
    pnl = current_daily_pnl(session, now=decision_timestamp)
    current_drawdown = max(Decimal("0"), -pnl)
    utilization = current_drawdown / max_daily_drawdown
    return (
        float(utilization),
        1.0,
        {
            "source": "paper_daily_pnl_vs_autopilot_max_daily_drawdown",
            "daily_pnl": decimal_to_str(pnl),
            "current_drawdown": decimal_to_str(current_drawdown),
            "max_daily_drawdown": decimal_to_str(max_daily_drawdown),
            "normalized_current_drawdown_fraction": decimal_to_str(utilization),
            "normalized_max_drawdown_fraction": "1",
        },
    )


def _remaining_position_cap(
    session: Session,
    *,
    ticker: str,
    side: str,
    settings: Settings,
) -> int:
    position = _pending_position(session, ticker) or session.get(PaperPosition, ticker)
    current = 0
    if position is not None:
        if side == BUY_YES:
            current = int(position.yes_contracts)
        elif side == BUY_NO:
            current = int(position.no_contracts)
    return max(0, settings.paper_max_position_per_market - current)


def _pending_position(session: Session, ticker: str) -> PaperPosition | None:
    for item in session.new:
        if isinstance(item, PaperPosition) and item.ticker == ticker:
            return item
    return None


def _snapshot_for_forecast(session: Session, forecast: Forecast) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(
            MarketSnapshot.ticker == forecast.ticker,
            MarketSnapshot.captured_at <= forecast.forecasted_at,
        )
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    ) or _latest_snapshot_for_ticker(session, forecast.ticker)


def _latest_snapshot_for_ticker(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _spread(snapshot: MarketSnapshot) -> Decimal | None:
    spread = to_decimal(snapshot.spread)
    if spread is not None:
        return spread
    bid = to_decimal(snapshot.best_yes_bid)
    ask = to_decimal(snapshot.best_yes_ask)
    if bid is not None and ask is not None:
        return ask - bid
    return None


def _time_to_close_minutes(
    snapshot: MarketSnapshot,
    raw_market: dict[str, Any],
) -> Decimal | None:
    close_time = parse_datetime(raw_market.get("close_time"))
    if close_time is None:
        return None
    captured_at = snapshot.captured_at
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    return Decimal(str((close_time - captured_at).total_seconds() / 60))


def _score_0_100_to_unit(value: Any) -> float | None:
    score = to_decimal(value)
    if score is None:
        return None
    return float(score / Decimal("100"))


def _trade_intent_id(decision: PaperDecision) -> str:
    if decision.forecast_id is not None:
        return f"forecast:{decision.forecast_id}"
    return f"{decision.ticker}:{decision.model_name}:{decision.side}"


def _existing_sizing_decision_id(raw_decision_json: dict[str, Any]) -> int | None:
    value = raw_decision_json.get("position_sizing_decision_id")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
