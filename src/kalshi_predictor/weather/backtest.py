from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.strategy import paper_decision_for_backtest
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.schema import Forecast, MarketSnapshot, Settlement
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.repository import get_weather_links


def run_weather_model_backtest(
    session: Session,
    *,
    model_name: str,
    days: int,
) -> dict[str, Any]:
    linked_tickers = {link.ticker for link in get_weather_links(session)}
    if not linked_tickers:
        return {
            "model_name": model_name,
            "linked_market_count": 0,
            "evaluated_forecasts": 0,
            "trades": [],
        }
    settings = get_settings()
    end_time = utc_now()
    start_time = end_time - timedelta(days=days)
    forecasts = list(
        session.scalars(
            select(Forecast)
            .where(
                Forecast.model_name == model_name,
                Forecast.ticker.in_(linked_tickers),
                Forecast.forecasted_at >= start_time,
                Forecast.forecasted_at <= end_time,
            )
            .order_by(Forecast.forecasted_at, Forecast.id)
        )
    )
    trades: list[dict[str, Any]] = []
    evaluated_forecasts = 0
    for forecast in forecasts:
        settlement = session.get(Settlement, forecast.ticker)
        y_true = _settlement_to_y_true(settlement.result if settlement else None)
        if y_true is None:
            continue
        evaluated_forecasts += 1
        snapshot = _snapshot_for_forecast(session, forecast)
        decision = paper_decision_for_backtest(forecast, snapshot, settings)
        if decision is None:
            continue
        trades.append(
            _trade_from_decision(
                decision,
                y_true=y_true,
                settlement_result=settlement.result if settlement else None,
                fee_per_contract=settings.paper_default_fee_per_contract,
            )
        )
    return {
        "model_name": model_name,
        "linked_market_count": len(linked_tickers),
        "evaluated_forecasts": evaluated_forecasts,
        "trades": trades,
    }


def _snapshot_for_forecast(session: Session, forecast: Forecast) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == forecast.ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _trade_from_decision(
    decision: Any,
    *,
    y_true: int,
    settlement_result: str | None,
    fee_per_contract: Decimal,
) -> dict[str, Any]:
    fee = fee_per_contract * Decimal(decision.quantity)
    exposure = decision.price * decision.quantity + fee
    if decision.side == BUY_YES:
        payout = Decimal(decision.quantity) if y_true == 1 else Decimal("0")
    elif decision.side == BUY_NO:
        payout = Decimal(decision.quantity) if y_true == 0 else Decimal("0")
    else:
        payout = Decimal("0")
    pnl = payout - exposure
    return {
        "ticker": decision.ticker,
        "forecast_id": decision.forecast_id,
        "simulated_at": decision.simulated_at.isoformat(),
        "side": decision.side,
        "price": decimal_to_str(decision.price) or "0",
        "quantity": decision.quantity,
        "edge": decimal_to_str(decision.edge) or "0",
        "settlement_result": settlement_result,
        "pnl": decimal_to_str(pnl) or "0",
        "exposure": decimal_to_str(exposure) or "0",
        "yes_probability": float(decision.yes_probability),
        "y_true": y_true,
    }


def _settlement_to_y_true(result: str | None) -> int | None:
    if result is None:
        return None
    normalized = result.strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return 1
    if normalized in {"no", "n", "0", "false"}:
        return 0
    return None
