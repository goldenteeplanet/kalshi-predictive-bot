from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.meta_model_v1 import _market_midpoint
from kalshi_predictor.meta.selector import select_model_for_ticker
from kalshi_predictor.utils.decimals import clamp_probability, to_decimal


class MetaEnsembleV1Forecaster:
    model_name = "meta_ensemble_v1"

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
        probabilities = _feature_dict(selection.feature, "model_probabilities")
        weights, reason = meta_ensemble_weights(
            probabilities=probabilities,
            trust_scores=selection.trust_scores,
            disagreement=to_decimal(
                _feature_value(selection.feature, "model_disagreement_score")
            )
            or Decimal("0"),
        )
        if not weights:
            return None
        probability = sum(
            (to_decimal(probabilities[model_name]) or Decimal("0")) * weight
            for model_name, weight in weights.items()
        )
        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=clamp_probability(probability),
            market_mid_probability=_market_midpoint(snapshot),
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                "component_weights": {key: str(value) for key, value in weights.items()},
                "component_probabilities": probabilities,
                "selected_model": selection.selected_model_name,
                "selected_trust_score": str(selection.selected_confidence),
                "model_disagreement_score": _feature_value(
                    selection.feature,
                    "model_disagreement_score",
                ),
                "reason": reason,
                "meta_decision_id": selection.decision_id,
            },
            notes="meta_ensemble_v1 blends stored models by local meta trust scores.",
        )


def meta_ensemble_weights(
    *,
    probabilities: dict[str, Any],
    trust_scores: dict[str, Any],
    disagreement: Decimal,
) -> tuple[dict[str, Decimal], str]:
    usable = {
        model_name: max(to_decimal(trust_scores.get(model_name)) or Decimal("0"), Decimal("0"))
        for model_name, probability in probabilities.items()
        if to_decimal(probability) is not None
    }
    if not usable:
        return {}, "No usable component probabilities."
    if disagreement >= Decimal("0.25") and "market_implied_v1" in usable:
        usable["market_implied_v1"] += Decimal("25")
        reason = "High model disagreement; increased market_implied_v1 stabilizer weight."
    elif disagreement <= Decimal("0.08"):
        reason = "Models mostly agree; trust-weighted smooth blend."
    else:
        reason = "Moderate model disagreement; trust-weighted blend."
    total = sum(usable.values(), Decimal("0"))
    if total <= 0:
        equal = Decimal("1") / Decimal(len(usable))
        return {model_name: equal for model_name in usable}, "No trust separation; equal blend."
    return {model_name: value / total for model_name, value in usable.items()}, reason


def _feature_dict(feature: dict[str, Any], key: str) -> dict[str, Any]:
    value = feature.get(key) or feature.get(f"{key}_json") or {}
    return value if isinstance(value, dict) else {}


def _feature_value(feature: dict[str, Any], key: str) -> Any:
    return feature.get(key) or (feature.get("raw_json") or {}).get(key)
