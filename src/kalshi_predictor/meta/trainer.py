from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Forecast, Market, Settlement
from kalshi_predictor.meta.repository import insert_meta_training_example
from kalshi_predictor.tournament.ranking import classify_market_category
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class MetaTrainingBuildSummary:
    settled_markets_scanned: int
    examples_inserted: int
    skipped_unsettled: int
    limited_comparisons: int


def build_meta_training_examples(
    session: Session,
    *,
    days: int = 90,
) -> MetaTrainingBuildSummary:
    session.flush()
    since = utc_now() - timedelta(days=days)
    settlements = list(
        session.scalars(
            select(Settlement).where(
                Settlement.updated_at >= since,
                Settlement.result.in_(("yes", "no")),
            )
        )
    )
    inserted = 0
    limited = 0
    skipped = 0
    for settlement in settlements:
        if settlement.result not in {"yes", "no"}:
            skipped += 1
            continue
        forecasts = _eligible_forecasts(session, settlement)
        if not forecasts:
            continue
        losses = {
            forecast.id: _brier_loss(forecast, settlement.result) for forecast in forecasts
        }
        best_loss = min(losses.values())
        if len(forecasts) == 1:
            limited += 1
        for forecast in forecasts:
            probability = to_decimal(forecast.yes_probability) or Decimal("0")
            actual = Decimal(1 if settlement.result == "yes" else 0)
            row = insert_meta_training_example(
                session,
                {
                    "ticker": forecast.ticker,
                    "forecast_id": forecast.id,
                    "model_name": forecast.model_name,
                    "category": _category_for_ticker(session, forecast.ticker),
                    "market_type": _market_type(session, forecast.ticker),
                    "predicted_probability": probability,
                    "settlement_result": settlement.result,
                    "absolute_error": abs(probability - actual),
                    "brier_loss": losses[forecast.id],
                    "was_best_model": losses[forecast.id] == best_loss,
                    "features": _features_for_training(forecast),
                    "raw_json": {
                        "settled_at": settlement.settled_at.isoformat()
                        if settlement.settled_at
                        else None,
                        "limited_comparison": len(forecasts) == 1,
                    },
                },
            )
            inserted += int(row is not None)
    return MetaTrainingBuildSummary(
        settled_markets_scanned=len(settlements),
        examples_inserted=inserted,
        skipped_unsettled=skipped,
        limited_comparisons=limited,
    )


def training_examples_by_ticker(session: Session) -> dict[str, list[dict[str, Any]]]:
    from kalshi_predictor.data.schema import MetaModelTrainingExample
    from kalshi_predictor.meta.repository import row_to_dict

    rows = session.scalars(select(MetaModelTrainingExample)).all()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        payload = row_to_dict(row)
        if payload is not None:
            grouped[row.ticker].append(payload)
    return grouped


def _eligible_forecasts(session: Session, settlement: Settlement) -> list[Forecast]:
    statement = select(Forecast).where(Forecast.ticker == settlement.ticker)
    if settlement.settled_at is not None:
        statement = statement.where(Forecast.forecasted_at <= settlement.settled_at)
    return list(session.scalars(statement.order_by(Forecast.forecasted_at, Forecast.id)))


def _brier_loss(forecast: Forecast, settlement_result: str) -> Decimal:
    probability = to_decimal(forecast.yes_probability) or Decimal("0")
    actual = Decimal(1 if settlement_result == "yes" else 0)
    return (probability - actual) ** 2


def _category_for_ticker(session: Session, ticker: str) -> str:
    market = session.get(Market, ticker)
    if market is None:
        return "unknown"
    text = " ".join(
        str(part or "")
        for part in (
            market.ticker,
            market.title,
            market.subtitle,
            market.series_ticker,
            market.event_ticker,
            market.rules_primary,
            market.rules_secondary,
        )
    )
    return classify_market_category(text)


def _market_type(session: Session, ticker: str) -> str | None:
    market = session.get(Market, ticker)
    return market.market_type if market is not None else None


def _features_for_training(forecast: Forecast) -> dict[str, Any]:
    return {
        "forecasted_at": forecast.forecasted_at.isoformat(),
        "model_name": forecast.model_name,
        "predicted_probability": forecast.yes_probability,
        "market_mid_probability": forecast.market_mid_probability,
        "forecast_feature_json": decode_json(forecast.feature_json),
        "leakage_guard": "settlement excluded from features",
    }
