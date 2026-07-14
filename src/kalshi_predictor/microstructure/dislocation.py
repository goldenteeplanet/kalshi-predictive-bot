from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import Forecast
from kalshi_predictor.utils.decimals import ONE_DOLLAR, to_decimal


def dislocation_score(
    *,
    market_midpoint: Decimal | None,
    model_probability: Decimal | None,
    recent_velocity: Decimal | None = None,
) -> Decimal:
    if market_midpoint is None or model_probability is None:
        return Decimal("0")
    divergence = abs(model_probability - market_midpoint)
    movement_bonus = abs(recent_velocity or Decimal("0")) / Decimal("2")
    return min(divergence + movement_bonus, Decimal("1"))


def detect_dislocation_events(
    session: Session,
    feature: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or get_settings()
    midpoint = _midpoint_from_feature(feature)
    forecast = _latest_forecast(session, str(feature["ticker"]), "ensemble_v2")
    model_probability = to_decimal(forecast.yes_probability if forecast else None)
    score = dislocation_score(
        market_midpoint=midpoint,
        model_probability=model_probability,
        recent_velocity=to_decimal(feature.get("price_velocity")),
    )
    if score < resolved_settings.microstructure_dislocation_threshold:
        return []
    events: list[dict[str, Any]] = []
    if midpoint is not None and model_probability is not None:
        direction = "YES" if model_probability > midpoint else "NO"
        events.append(
            {
                "ticker": feature["ticker"],
                "event_type": f"PRICE_DISLOCATION_{direction}",
                "severity": "HIGH" if score >= Decimal("0.12") else "MEDIUM",
                "score": score * Decimal("100"),
                "title": f"Price Dislocation {direction}",
                "description": (
                    f"ensemble_v2 estimates {model_probability:.2%}, while market "
                    f"is priced near {midpoint:.2%}."
                ),
                "evidence": {
                    "market_midpoint": str(midpoint),
                    "ensemble_v2_probability": str(model_probability),
                    "price_velocity": str(feature.get("price_velocity")),
                },
            }
        )
        events.append(
            {
                **events[-1],
                "event_type": "MODEL_MARKET_DIVERGENCE",
                "title": "Model Market Divergence",
            }
        )
    disagreement = _cross_model_disagreement(session, str(feature["ticker"]))
    if (
        disagreement is not None
        and disagreement["spread"] >= resolved_settings.microstructure_dislocation_threshold
    ):
        events.append(
            {
                "ticker": feature["ticker"],
                "event_type": "CROSS_MODEL_DISAGREEMENT",
                "severity": "MEDIUM",
                "score": min(disagreement["spread"] * Decimal("100"), Decimal("100")),
                "title": "Cross Model Disagreement",
                "description": (
                    "Recent model probabilities disagree, so microstructure should be "
                    "used as a cautious tie-breaker only."
                ),
                "evidence": disagreement,
            }
        )
    return events


def _latest_forecast(session: Session, ticker: str, model_name: str) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == model_name)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _cross_model_disagreement(session: Session, ticker: str) -> dict[str, Any] | None:
    rows = list(
        session.scalars(
            select(Forecast)
            .where(Forecast.ticker == ticker)
            .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
            .limit(50)
        )
    )
    latest_by_model: dict[str, Forecast] = {}
    for row in rows:
        if row.model_name in latest_by_model:
            continue
        latest_by_model[row.model_name] = row
    probabilities = {
        model_name: probability
        for model_name, row in latest_by_model.items()
        if (probability := to_decimal(row.yes_probability)) is not None
    }
    if len(probabilities) < 2:
        return None
    values = list(probabilities.values())
    return {
        "spread": max(values) - min(values),
        "probabilities": {model_name: str(value) for model_name, value in probabilities.items()},
    }


def _midpoint_from_feature(feature: dict[str, Any]) -> Decimal | None:
    bid = to_decimal(feature.get("current_yes_bid"))
    ask = to_decimal(feature.get("current_yes_ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / Decimal("2")
    no_bid = to_decimal(feature.get("current_no_bid"))
    if no_bid is not None:
        return ONE_DOLLAR - no_bid
    return None
