import math
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    Forecast,
    MarketRanking,
    PaperOrder,
    PaperPnl,
    Settlement,
    Signal,
    SignalForecast,
    SignalPerformance,
    SignalTrade,
)
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.signals.registry import ensure_builtin_signals
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


def refresh_signal_performance(session: Session) -> list[SignalPerformance]:
    signals = ensure_builtin_signals(session)
    rows = [calculate_signal_performance(session, signal) for signal in signals]
    session.flush()
    return rows


def calculate_signal_performance(session: Session, signal: Signal) -> SignalPerformance:
    forecasts = _forecasts_for_signal(session, signal.signal_name)
    orders = _orders_for_signal(session, signal.signal_name)
    forecast_count = len(forecasts)
    trade_count = len(orders)
    settled_results = [
        result
        for result in (_settlement_result(session, order.ticker) for order in orders)
        if result is not None
    ]
    settled_trade_count = len(settled_results)
    wins = 0
    total_pnl = Decimal("0")
    total_exposure = Decimal("0")
    edges: list[Decimal] = []
    opportunity_scores: list[Decimal] = []

    for order in orders:
        exposure = (to_decimal(order.limit_price) or Decimal("0")) * Decimal(order.quantity)
        total_exposure += exposure
        edge = to_decimal(order.edge)
        if edge is not None:
            edges.append(edge)
        opportunity_score = _latest_opportunity_score(session, order)
        if opportunity_score is not None:
            opportunity_scores.append(opportunity_score)
        result = _settlement_result(session, order.ticker)
        if result is None:
            total_pnl += _latest_mark_to_market_pnl(session, order.ticker)
            continue
        pnl = _settled_order_pnl(order, result)
        total_pnl += pnl
        if _order_won(order, result):
            wins += 1

    brier_values: list[Decimal] = []
    log_loss_values: list[Decimal] = []
    for forecast in forecasts:
        result = _settlement_result(session, forecast.ticker)
        if result is None:
            continue
        probability = to_decimal(forecast.yes_probability)
        if probability is None:
            continue
        outcome = Decimal("1") if result == "yes" else Decimal("0")
        brier_values.append((probability - outcome) ** 2)
        log_loss_values.append(_log_loss(probability, outcome))

    win_rate = (
        Decimal(wins) / Decimal(settled_trade_count)
        if settled_trade_count > 0
        else None
    )
    roi = total_pnl / total_exposure if total_exposure > 0 else None
    brier = _average(brier_values)
    log_loss = _average(log_loss_values)
    avg_edge = _average(edges)
    avg_opportunity_score = _average(opportunity_scores)
    confidence = _confidence_score(
        forecast_count=forecast_count,
        trade_count=trade_count,
        settled_trade_count=settled_trade_count,
        roi=roi,
        brier=brier,
    )
    raw = {
        "wins": wins,
        "total_exposure": decimal_to_str(total_exposure),
        "status": signal_status(
            forecast_count=forecast_count,
            trade_count=trade_count,
            roi=roi,
            win_rate=win_rate,
            confidence_score=confidence,
        ),
    }
    row = SignalPerformance(
        generated_at=utc_now(),
        signal_name=signal.signal_name,
        category=signal.category,
        forecast_count=forecast_count,
        trade_count=trade_count,
        settled_trade_count=settled_trade_count,
        win_rate=decimal_to_str(win_rate),
        total_pnl=decimal_to_str(total_pnl),
        roi=decimal_to_str(roi),
        avg_edge=decimal_to_str(avg_edge),
        avg_opportunity_score=decimal_to_str(avg_opportunity_score),
        brier_score=decimal_to_str(brier),
        log_loss=decimal_to_str(log_loss),
        confidence_score=decimal_to_str(confidence),
        raw_json=encode_json(raw),
    )
    signal.status = raw["status"]
    session.add(row)
    return row


def signal_status(
    *,
    forecast_count: int,
    trade_count: int,
    roi: Decimal | None,
    win_rate: Decimal | None,
    confidence_score: Decimal | None,
) -> str:
    if forecast_count < 5 and trade_count < 3:
        return "Insufficient Data"
    confidence = confidence_score or Decimal("0")
    resolved_roi = roi or Decimal("0")
    resolved_win_rate = win_rate or Decimal("0")
    if confidence >= Decimal("70") and resolved_roi > 0 and resolved_win_rate >= Decimal("0.50"):
        return "Strong"
    if confidence >= Decimal("45") and resolved_roi >= Decimal("-0.05"):
        return "Average"
    return "Weak"


def performance_rank_key(row: SignalPerformance) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    roi = to_decimal(row.roi) or Decimal("-999")
    confidence = to_decimal(row.confidence_score) or Decimal("0")
    brier = to_decimal(row.brier_score)
    calibration = Decimal("1") - brier if brier is not None else Decimal("0")
    sample = Decimal(row.forecast_count) + Decimal(row.trade_count * 3)
    return roi, min(sample, Decimal("100")), calibration, confidence


def _forecasts_for_signal(session: Session, signal_name: str) -> list[Forecast]:
    return list(
        session.scalars(
            select(Forecast)
            .join(SignalForecast, SignalForecast.forecast_id == Forecast.id)
            .where(SignalForecast.signal_name == signal_name)
            .order_by(Forecast.forecasted_at, Forecast.id)
        )
    )


def _orders_for_signal(session: Session, signal_name: str) -> list[PaperOrder]:
    return list(
        session.scalars(
            select(PaperOrder)
            .join(SignalTrade, SignalTrade.paper_order_id == PaperOrder.id)
            .where(SignalTrade.signal_name == signal_name)
            .order_by(PaperOrder.created_at, PaperOrder.id)
        )
    )


def _settlement_result(session: Session, ticker: str) -> str | None:
    settlement = session.get(Settlement, ticker)
    if settlement is not None and settlement.result:
        return _normalized_result(settlement.result)
    pnl = session.scalar(
        select(PaperPnl)
        .where(PaperPnl.ticker == ticker, PaperPnl.settlement_result.is_not(None))
        .order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id))
    )
    return _normalized_result(pnl.settlement_result) if pnl else None


def _latest_mark_to_market_pnl(session: Session, ticker: str) -> Decimal:
    pnl = session.scalar(
        select(PaperPnl)
        .where(PaperPnl.ticker == ticker)
        .order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id))
    )
    return to_decimal(pnl.total_pnl if pnl else None) or Decimal("0")


def _latest_opportunity_score(session: Session, order: PaperOrder) -> Decimal | None:
    ranking = session.scalar(
        select(MarketRanking)
        .where(
            MarketRanking.ticker == order.ticker,
            MarketRanking.forecast_model == order.model_name,
        )
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
    )
    return to_decimal(ranking.opportunity_score if ranking else None)


def _settled_order_pnl(order: PaperOrder, result: str) -> Decimal:
    price = to_decimal(order.limit_price) or to_decimal(order.market_price) or Decimal("0")
    quantity = Decimal(order.quantity)
    if _order_won(order, result):
        return (Decimal("1") - price) * quantity
    return -price * quantity


def _order_won(order: PaperOrder, result: str) -> bool:
    if order.side == BUY_YES:
        return result == "yes"
    if order.side == BUY_NO:
        return result == "no"
    return False


def _normalized_result(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return "yes"
    if normalized in {"no", "n", "0", "false"}:
        return "no"
    return None


def _log_loss(probability: Decimal, outcome: Decimal) -> Decimal:
    clipped = min(max(float(probability), 0.001), 0.999)
    resolved_outcome = float(outcome)
    loss = -(
        resolved_outcome * math.log(clipped)
        + (1 - resolved_outcome) * math.log(1 - clipped)
    )
    return Decimal(str(loss))


def _average(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _confidence_score(
    *,
    forecast_count: int,
    trade_count: int,
    settled_trade_count: int,
    roi: Decimal | None,
    brier: Decimal | None,
) -> Decimal:
    sample_score = min(
        Decimal(forecast_count * 2 + trade_count * 5 + settled_trade_count * 10),
        Decimal("60"),
    )
    roi_score = Decimal("0")
    if roi is not None:
        roi_score = max(min(roi * Decimal("100"), Decimal("20")), Decimal("-20"))
    calibration_score = Decimal("0")
    if brier is not None:
        calibration_score = max(Decimal("0"), Decimal("20") - brier * Decimal("40"))
    confidence = sample_score + roi_score + calibration_score
    if confidence < Decimal("0"):
        return Decimal("0")
    if confidence > Decimal("100"):
        return Decimal("100")
    return confidence
