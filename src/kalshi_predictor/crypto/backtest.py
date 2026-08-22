from datetime import timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.strategy import paper_decision_for_backtest
from kalshi_predictor.config import get_settings
from kalshi_predictor.crypto.repository import get_crypto_links
from kalshi_predictor.data.schema import Forecast, MarketSnapshot, Settlement
from kalshi_predictor.historical_replay_common import (
    settlement_to_y_true as _settlement_to_y_true,
)
from kalshi_predictor.historical_replay_common import (
    trade_from_decision as _trade_from_decision,
)
from kalshi_predictor.utils.time import utc_now


def run_crypto_model_backtest(
    session: Session,
    *,
    model_name: str,
    days: int,
) -> dict[str, Any]:
    linked_tickers = {link.ticker for link in get_crypto_links(session)}
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
        .where(
            MarketSnapshot.ticker == forecast.ticker,
            MarketSnapshot.captured_at <= forecast.forecasted_at,
        )
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )
