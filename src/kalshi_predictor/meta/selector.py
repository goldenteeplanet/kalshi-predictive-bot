from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import MarketSnapshot, SignalEvent
from kalshi_predictor.meta.feature_builder import (
    META_CANDIDATE_MODELS,
    build_meta_features_for_ticker,
)
from kalshi_predictor.meta.repository import (
    insert_meta_model_decision,
    latest_meta_feature,
    row_to_dict,
)
from kalshi_predictor.signals.registry import ensure_builtin_signals
from kalshi_predictor.signals.signal_types import (
    FALLBACK_SIGNAL,
    META_SELECTION_SIGNAL,
    MODEL_DISAGREEMENT_SIGNAL,
    MODEL_TRUST_SIGNAL,
    SPECIALIZED_MODEL_ADVANTAGE_SIGNAL,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

MIN_TRUST_SCORE = Decimal("45")


@dataclass(frozen=True)
class MetaSelection:
    ticker: str
    selected_model_name: str
    selected_probability: Decimal
    selected_confidence: Decimal
    fallback_model_name: str | None
    decision_reason: str
    competing_models: dict[str, Any]
    trust_scores: dict[str, str]
    feature: dict[str, Any]
    decision_id: int | None = None


@dataclass(frozen=True)
class MetaSelectionSummary:
    markets_scanned: int
    decisions_inserted: int
    skipped: int


def select_models_for_recent_markets(
    session: Session,
    *,
    limit: int = 100,
) -> MetaSelectionSummary:
    from kalshi_predictor.meta.feature_builder import _snapshots

    snapshots = _snapshots(session, ticker=None, limit=limit)
    inserted = 0
    skipped = 0
    for snapshot in snapshots:
        selection = select_model_for_ticker(
            session,
            ticker=snapshot.ticker,
            snapshot=snapshot,
            persist=True,
        )
        if selection is None:
            skipped += 1
        else:
            inserted += 1
    return MetaSelectionSummary(
        markets_scanned=len(snapshots),
        decisions_inserted=inserted,
        skipped=skipped,
    )


def select_model_for_ticker(
    session: Session,
    *,
    ticker: str,
    snapshot: MarketSnapshot | None = None,
    persist: bool = True,
) -> MetaSelection | None:
    feature = _feature_for_selection(session, ticker=ticker, snapshot=snapshot, persist=persist)
    if feature is None:
        return None
    probabilities = _json_field(feature, "model_probabilities")
    if not probabilities:
        return None
    trust_scores = score_candidate_models(feature)
    selected_model, fallback_model = _select_model(probabilities, trust_scores)
    if selected_model is None:
        return None
    selected_probability = to_decimal(probabilities.get(selected_model))
    if selected_probability is None:
        return None
    selected_confidence = trust_scores.get(selected_model, Decimal("0"))
    reason = _decision_reason(
        feature=feature,
        selected_model=selected_model,
        selected_confidence=selected_confidence,
        fallback_model=fallback_model,
    )
    competing_models = {
        model_name: {
            "probability": str(probabilities.get(model_name)),
            "trust_score": decimal_to_str(score) or "0",
        }
        for model_name, score in trust_scores.items()
        if model_name in probabilities
    }
    row = None
    if persist:
        row = insert_meta_model_decision(
            session,
            {
                "ticker": ticker,
                "selected_model_name": selected_model,
                "selected_probability": selected_probability,
                "selected_confidence": selected_confidence,
                "fallback_model_name": fallback_model,
                "decision_reason": reason,
                "competing_models": competing_models,
                "trust_scores": {
                    model_name: decimal_to_str(score) or "0"
                    for model_name, score in trust_scores.items()
                },
                "raw_json": {
                    "feature_id": feature.get("id"),
                    "category": feature.get("category"),
                    "model_disagreement_score": _json_value(
                        feature,
                        "model_disagreement_score",
                    ),
                    "selected_model_name": selected_model,
                    "selected_probability": decimal_to_str(selected_probability),
                    "selected_confidence": decimal_to_str(selected_confidence),
                    "fallback_model_name": fallback_model,
                    "decision_reason": reason,
                    "competing_models": competing_models,
                },
            },
        )
        _record_meta_signal_events(session, feature=feature, selection=row_to_dict(row) or {})
    return MetaSelection(
        ticker=ticker,
        selected_model_name=selected_model,
        selected_probability=selected_probability,
        selected_confidence=selected_confidence,
        fallback_model_name=fallback_model,
        decision_reason=reason,
        competing_models=competing_models,
        trust_scores={
            model_name: decimal_to_str(score) or "0"
            for model_name, score in trust_scores.items()
        },
        feature=feature,
        decision_id=row.id if row is not None else None,
    )


def score_candidate_models(feature: dict[str, Any]) -> dict[str, Decimal]:
    probabilities = _json_field(feature, "model_probabilities")
    category = str(feature.get("category") or "general")
    active_signals = _json_field(feature, "active_signals", default=[])
    model_performance = _json_field(feature, "model_recent_performance")
    disagreement = to_decimal(_json_value(feature, "model_disagreement_score")) or Decimal("0")
    freshness = to_decimal(feature.get("data_freshness_score")) or Decimal("0")
    spread_score = to_decimal(feature.get("spread_score")) or Decimal("0")
    liquidity_score = to_decimal(feature.get("liquidity_score")) or Decimal("0")
    scores: dict[str, Decimal] = {}
    for model_name in META_CANDIDATE_MODELS:
        if model_name not in probabilities:
            continue
        probability = to_decimal(probabilities.get(model_name))
        if probability is None:
            continue
        score = Decimal("30")
        score += Decimal("15")
        affinity = _category_affinity(category, model_name)
        score += Decimal("20") * affinity
        if affinity == 0 and model_name not in {"ensemble_v1", "ensemble_v2", "market_implied_v1"}:
            score -= Decimal("20")
        performance = model_performance.get(model_name, {})
        score += _performance_score(performance)
        score += _signal_support_score(model_name, active_signals)
        score += freshness * Decimal("0.05")
        if freshness < Decimal("40"):
            score -= Decimal("10")
        score += _agreement_adjustment(model_name, probability, probabilities, disagreement)
        score += _specialized_feature_adjustment(feature, category, model_name)
        if spread_score < Decimal("30") and model_name in {
            "microstructure_v1",
            "market_implied_v1",
        }:
            score -= Decimal("10")
        if liquidity_score < Decimal("10") and model_name in {
            "microstructure_v1",
            "market_implied_v1",
        }:
            score -= Decimal("10")
        scores[model_name] = _clamp(score)
    return scores


def _feature_for_selection(
    session: Session,
    *,
    ticker: str,
    snapshot: MarketSnapshot | None,
    persist: bool,
) -> dict[str, Any] | None:
    if snapshot is not None:
        return build_meta_features_for_ticker(
            session,
            ticker=ticker,
            snapshot=snapshot,
            persist=persist,
        )
    existing = latest_meta_feature(session, ticker)
    if existing is not None:
        return row_to_dict(existing)
    return build_meta_features_for_ticker(session, ticker=ticker, persist=persist)


def _select_model(
    probabilities: dict[str, Any],
    trust_scores: dict[str, Decimal],
) -> tuple[str | None, str | None]:
    available = {
        model_name: trust_scores.get(model_name, Decimal("0"))
        for model_name in probabilities
        if to_decimal(probabilities.get(model_name)) is not None
    }
    if not available:
        return None, None
    best_model = max(available, key=lambda model: available[model])
    if available[best_model] >= MIN_TRUST_SCORE:
        return best_model, None
    for fallback in ("ensemble_v2", "market_implied_v1"):
        if fallback in probabilities and to_decimal(probabilities[fallback]) is not None:
            return fallback, fallback
    return best_model, best_model


def _performance_score(performance: dict[str, Any]) -> Decimal:
    score = Decimal("0")
    confidence = to_decimal(performance.get("confidence_score"))
    if confidence is not None:
        score += confidence * Decimal("0.20")
    settled = int(performance.get("settled_trade_count") or 0)
    if settled < 5:
        score -= Decimal("10")
    roi = to_decimal(performance.get("roi_on_exposure"))
    if roi is not None:
        if roi >= 0:
            score += min(Decimal("10"), roi * Decimal("20"))
        else:
            score += max(Decimal("-15"), roi * Decimal("25"))
    brier = to_decimal(performance.get("brier_score"))
    if brier is not None:
        score += max(Decimal("-10"), (Decimal("0.25") - brier) * Decimal("40"))
    return score


def _signal_support_score(model_name: str, active_signals: list[dict[str, Any]]) -> Decimal:
    if not active_signals:
        return Decimal("0")
    categories = " ".join(
        str(row.get("category") or row.get("signal_name") or "")
        for row in active_signals
    )
    categories = categories.lower()
    if "crypto" in model_name and "crypto" in categories:
        return Decimal("10")
    if "weather" in model_name and "weather" in categories:
        return Decimal("10")
    if _is_sports_model(model_name) and "sports" in categories:
        return Decimal("10")
    if "news" in model_name and "news" in categories:
        return Decimal("10")
    if "economic" in model_name and "economic" in categories:
        return Decimal("10")
    if "microstructure" in model_name and "microstructure" in categories:
        return Decimal("12")
    if "ensemble" in model_name and ("model" in categories or "agreement" in categories):
        return Decimal("5")
    return Decimal("0")


def _agreement_adjustment(
    model_name: str,
    probability: Decimal,
    probabilities: dict[str, Any],
    disagreement: Decimal,
) -> Decimal:
    numeric = [
        value
        for value in (to_decimal(item) for item in probabilities.values())
        if value is not None
    ]
    if len(numeric) < 2:
        return Decimal("0")
    yes_votes = sum(1 for value in numeric if value >= Decimal("0.5"))
    majority_yes = yes_votes >= len(numeric) - yes_votes
    model_yes = probability >= Decimal("0.5")
    if model_yes == majority_yes:
        return Decimal("5")
    if disagreement >= Decimal("0.20"):
        return Decimal("-8")
    if model_name.startswith("ensemble"):
        return Decimal("0")
    return Decimal("-3")


def _specialized_feature_adjustment(
    feature: dict[str, Any],
    category: str,
    model_name: str,
) -> Decimal:
    if model_name == "microstructure_v1":
        micro = _json_field(feature, "microstructure_features")
        confidence = to_decimal(micro.get("microstructure_confidence"))
        if confidence is not None:
            return min(Decimal("15"), confidence / Decimal("6"))
        return Decimal("-8")
    if category == "crypto" and model_name == "crypto_v2":
        return Decimal("12") if _json_field(feature, "crypto_features") else Decimal("-12")
    if category == "weather" and model_name == "weather_v2":
        return Decimal("12") if _json_field(feature, "weather_features") else Decimal("-12")
    if category == "sports" and _is_sports_model(model_name):
        return Decimal("12") if _json_field(feature, "sports_features") else Decimal("-12")
    if category == "economic" and _is_economic_model(model_name):
        return Decimal("10") if _json_field(feature, "economic_features") else Decimal("-8")
    if model_name == "news_v1":
        return Decimal("8") if _json_field(feature, "news_features") else Decimal("-5")
    return Decimal("0")


def _category_affinity(category: str, model_name: str) -> Decimal:
    if model_name in {"market_implied_v1", "ensemble_v1", "ensemble_v2"}:
        return Decimal("0.5")
    if category == "crypto" and model_name == "crypto_v2":
        return Decimal("1")
    if category == "weather" and model_name == "weather_v2":
        return Decimal("1")
    if category == "sports" and _is_sports_model(model_name):
        return Decimal("1")
    if category == "economic" and _is_economic_model(model_name):
        return Decimal("1")
    if category == "general" and model_name in {"news_v1", "microstructure_v1"}:
        return Decimal("0.5")
    return Decimal("0")


def _decision_reason(
    *,
    feature: dict[str, Any],
    selected_model: str,
    selected_confidence: Decimal,
    fallback_model: str | None,
) -> str:
    category = str(feature.get("category") or "general")
    if fallback_model:
        return (
            f"Falling back to {fallback_model} because no candidate cleared the trust "
            "threshold with enough local evidence."
        )
    reasons = [f"{selected_model} has the highest trust score for this {category} market"]
    if _category_affinity(category, selected_model) >= Decimal("1"):
        reasons.append("it matches the market category")
    if _json_field(feature, "active_signals"):
        reasons.append("active local signals support the selection")
    disagreement = to_decimal(_json_value(feature, "model_disagreement_score")) or Decimal("0")
    if disagreement >= Decimal("0.20"):
        reasons.append("models disagree materially, so trust scoring matters")
    reasons.append(f"trust is {selected_confidence.quantize(Decimal('1'))}/100")
    return "; ".join(reasons) + "."


def _record_meta_signal_events(
    session: Session,
    *,
    feature: dict[str, Any],
    selection: dict[str, Any],
) -> None:
    ensure_builtin_signals(session)
    selected = str(selection.get("selected_model_name") or "")
    trust = to_decimal(selection.get("selected_confidence")) or Decimal("0")
    disagreement = to_decimal(_json_value(feature, "model_disagreement_score")) or Decimal("0")
    events = [
        (
            META_SELECTION_SIGNAL,
            "selected",
            trust,
            f"Meta model selected {selected}.",
        )
    ]
    if trust >= Decimal("70"):
        events.append((MODEL_TRUST_SIGNAL, "trusted", trust, f"{selected} trust is high."))
    if disagreement >= Decimal("0.20"):
        events.append(
            (
                MODEL_DISAGREEMENT_SIGNAL,
                "disagreement",
                min(disagreement * Decimal("300"), Decimal("100")),
                "Candidate model probabilities materially disagree.",
            )
        )
    if selection.get("fallback_model_name"):
        events.append(
            (
                FALLBACK_SIGNAL,
                "fallback",
                Decimal("70"),
                f"Fallback model {selection['fallback_model_name']} was used.",
            )
        )
    if selected not in {"market_implied_v1", "ensemble_v1", "ensemble_v2"}:
        events.append(
            (
                SPECIALIZED_MODEL_ADVANTAGE_SIGNAL,
                "specialized",
                trust,
                f"{selected} has a specialized-model advantage.",
            )
        )
    for signal_name, direction, strength, message in events:
        session.add(
            SignalEvent(
                created_at=utc_now(),
                ticker=str(selection["ticker"]),
                signal_name=signal_name,
                model_name="meta_model_v1",
                signal_strength=decimal_to_str(_clamp(strength)) or "0",
                signal_value=selected,
                signal_direction=direction,
                confidence=decimal_to_str(_clamp(trust or strength)) or "0",
                raw_json=encode_json(
                    {
                        "source": "meta_selector",
                        "decision_id": selection.get("id"),
                        "message": message,
                    }
                ),
            )
        )


def _json_field(
    feature: dict[str, Any],
    name: str,
    default: Any | None = None,
) -> Any:
    direct = feature.get(name)
    if direct is not None:
        return direct
    value = feature.get(f"{name}_json")
    if value is not None:
        return value
    return {} if default is None else default


def _json_value(feature: dict[str, Any], name: str) -> Any:
    value = feature.get(name)
    if value is not None:
        return value
    raw = _json_field(feature, "raw")
    return raw.get(name) if isinstance(raw, dict) else None


def _is_sports_model(model_name: str) -> bool:
    return model_name in {"sports_v1", "mlb_v1", "nba_v1", "nfl_v1", "nhl_v1"}


def _is_economic_model(model_name: str) -> bool:
    return model_name in {"economic_v1", "economic_v2", "cpi_v1", "jobs_v1", "fed_v1", "gdp_v1"}


def _clamp(value: Decimal) -> Decimal:
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("100"):
        return Decimal("100")
    return value
