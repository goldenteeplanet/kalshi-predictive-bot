from decimal import Decimal

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Forecast, MarketSnapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.tournament.ranking import classify_market_category
from kalshi_predictor.tournament.repository import get_latest_model_weights
from kalshi_predictor.utils.decimals import midpoint, to_decimal

COMPONENT_MODELS = (
    "market_implied_v1",
    "crypto_v2",
    "weather_v2",
    "economic_v1",
    "news_v1",
    "sports_v1",
)


class EnsembleV2Forecaster:
    model_name = "ensemble_v2"

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        session.flush()
        components = _latest_component_forecasts(session, snapshot)
        if not components:
            return None
        category = _snapshot_category(snapshot)
        weight_records = get_latest_model_weights(session, category=category)
        weights = {
            record.model_name: to_decimal(record.weight) or Decimal("0")
            for record in weight_records
        }
        if not weights and category != "general":
            weights = {
                record.model_name: to_decimal(record.weight) or Decimal("0")
                for record in get_latest_model_weights(session, category="general")
            }

        weighted_probability, weights_used, fallback_reason = _weighted_probability(
            components,
            weights,
        )
        if weighted_probability is None:
            return None

        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=_clamp_probability(weighted_probability),
            market_mid_probability=_market_midpoint(snapshot),
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                "component_forecasts": {
                    model_name: {
                        "forecast_id": forecast.id,
                        "yes_probability": forecast.yes_probability,
                        "forecasted_at": forecast.forecasted_at.isoformat(),
                    }
                    for model_name, forecast in components.items()
                },
                "weights_used": {key: str(value) for key, value in weights_used.items()},
                "category": category,
                "weighted_probability": str(weighted_probability),
                "fallback_reason": fallback_reason,
            },
            notes="ensemble_v2 weighted average of stored component forecasts.",
        )


def _latest_component_forecasts(
    session: Session,
    snapshot: MarketSnapshot,
) -> dict[str, Forecast]:
    components: dict[str, Forecast] = {}
    for model_name in COMPONENT_MODELS:
        forecast = session.scalar(
            select(Forecast)
            .where(
                Forecast.ticker == snapshot.ticker,
                Forecast.model_name == model_name,
                Forecast.forecasted_at <= snapshot.captured_at,
            )
            .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
            .limit(1)
        )
        if forecast is not None:
            components[model_name] = forecast
    return components


def _weighted_probability(
    components: dict[str, Forecast],
    weights: dict[str, Decimal],
) -> tuple[Decimal | None, dict[str, Decimal], str | None]:
    component_probabilities = {
        model_name: to_decimal(forecast.yes_probability)
        for model_name, forecast in components.items()
    }
    component_probabilities = {
        model_name: probability
        for model_name, probability in component_probabilities.items()
        if probability is not None
    }
    if not component_probabilities:
        return None, {}, "no_component_probabilities"

    usable_weights = {
        model_name: weight
        for model_name, weight in weights.items()
        if model_name in component_probabilities and weight > 0
    }
    total_weight = sum(usable_weights.values(), Decimal("0"))
    if not usable_weights or total_weight <= 0:
        equal_weight = Decimal("1") / Decimal(len(component_probabilities))
        return (
            sum(component_probabilities.values(), Decimal("0"))
            / Decimal(len(component_probabilities)),
            {model_name: equal_weight for model_name in component_probabilities},
            "no_category_weights; used simple average",
        )

    normalized_weights = {
        model_name: weight / total_weight for model_name, weight in usable_weights.items()
    }
    probability = sum(
        component_probabilities[model_name] * weight
        for model_name, weight in normalized_weights.items()
    )
    return probability, normalized_weights, None


def _snapshot_category(snapshot: MarketSnapshot) -> str:
    raw_market = decode_json(snapshot.raw_market_json)
    text = " ".join(
        str(raw_market.get(key) or "")
        for key in ("ticker", "title", "subtitle", "series_ticker", "event_ticker", "rules")
    )
    return classify_market_category(text)


def _market_midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    if yes_bid is not None and yes_ask is not None:
        return midpoint(yes_bid, yes_ask)
    return to_decimal(snapshot.last_price_dollars)


def _clamp_probability(value: Decimal) -> Decimal:
    if value < Decimal("0.01"):
        return Decimal("0.01")
    if value > Decimal("0.99"):
        return Decimal("0.99")
    return value
