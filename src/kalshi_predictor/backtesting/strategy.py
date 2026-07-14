from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import Forecast, MarketSnapshot
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.utils.decimals import ONE_DOLLAR, to_decimal


@dataclass(frozen=True)
class BacktestDecision:
    ticker: str
    forecast_id: int
    simulated_at: object
    side: str
    price: Decimal
    quantity: int
    edge: Decimal
    yes_probability: Decimal
    raw_decision_json: dict[str, Any]


def paper_decision_for_backtest(
    forecast: Forecast,
    snapshot: MarketSnapshot | None,
    settings: Settings,
) -> BacktestDecision | None:
    if forecast.id is None or snapshot is None:
        return None
    yes_probability = to_decimal(forecast.yes_probability)
    if yes_probability is None:
        return None

    candidates: list[BacktestDecision] = []
    yes_ask = to_decimal(forecast.best_yes_ask) or to_decimal(snapshot.best_yes_ask)
    if yes_ask is not None:
        yes_edge = yes_probability - yes_ask
        if yes_edge >= settings.paper_min_edge:
            candidates.append(
                _decision(
                    forecast=forecast,
                    side=BUY_YES,
                    price=yes_ask,
                    edge=yes_edge,
                    yes_probability=yes_probability,
                    quantity=settings.paper_max_order_quantity,
                )
            )

    no_ask = to_decimal(snapshot.best_no_ask)
    if settings.paper_allow_buy_no and no_ask is not None:
        no_probability = ONE_DOLLAR - yes_probability
        no_edge = no_probability - no_ask
        if no_edge >= settings.paper_min_edge:
            candidates.append(
                _decision(
                    forecast=forecast,
                    side=BUY_NO,
                    price=no_ask,
                    edge=no_edge,
                    yes_probability=yes_probability,
                    quantity=settings.paper_max_order_quantity,
                )
            )

    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.edge)


def _decision(
    *,
    forecast: Forecast,
    side: str,
    price: Decimal,
    edge: Decimal,
    yes_probability: Decimal,
    quantity: int,
) -> BacktestDecision:
    return BacktestDecision(
        ticker=forecast.ticker,
        forecast_id=int(forecast.id),
        simulated_at=forecast.forecasted_at,
        side=side,
        price=price,
        quantity=quantity,
        edge=edge,
        yes_probability=yes_probability,
        raw_decision_json={
            "ticker": forecast.ticker,
            "forecast_id": forecast.id,
            "model_name": forecast.model_name,
            "forecasted_at": forecast.forecasted_at.isoformat(),
            "side": side,
            "price": str(price),
            "quantity": quantity,
            "edge": str(edge),
            "yes_probability": str(yes_probability),
            "strategy": "paper_v1",
        },
    )

