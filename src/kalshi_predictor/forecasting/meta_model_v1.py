from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.meta.selector import select_model_for_ticker
from kalshi_predictor.utils.decimals import clamp_probability, midpoint, to_decimal


class MetaModelV1Forecaster:
    model_name = "meta_model_v1"

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        session.flush()
        selection = select_model_for_ticker(
            session,
            ticker=snapshot.ticker,
            snapshot=snapshot,
            persist=True,
        )
        if selection is None:
            return None
        probability = selection.selected_probability
        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=clamp_probability(probability),
            market_mid_probability=_market_midpoint(snapshot),
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                "selected_model": selection.selected_model_name,
                "selected_probability": str(selection.selected_probability),
                "selected_trust_score": str(selection.selected_confidence),
                "competing_model_scores": selection.trust_scores,
                "competing_models": selection.competing_models,
                "fallback_model_name": selection.fallback_model_name,
                "reason": selection.decision_reason,
                "meta_decision_id": selection.decision_id,
                "category": selection.feature.get("category"),
                "model_disagreement_score": _feature_value(
                    selection.feature,
                    "model_disagreement_score",
                ),
            },
            notes=(
                "meta_model_v1 selects the locally most trustworthy stored model, "
                "paper/demo only."
            ),
        )


def _market_midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    if yes_bid is not None and yes_ask is not None:
        return midpoint(yes_bid, yes_ask)
    return to_decimal(snapshot.last_price_dollars)


def _feature_value(feature: dict[str, Any], key: str) -> Any:
    return feature.get(key) or (feature.get("raw_json") or {}).get(key)
