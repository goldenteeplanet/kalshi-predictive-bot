import json
from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.consensus.scoring import (
    assess_forum_consensus,
    assessment_to_dict,
)
from kalshi_predictor.explain.model_explainer import explain_model
from kalshi_predictor.explain.risk_explainer import data_freshness, explain_risks
from kalshi_predictor.explain.signal_explainer import primary_driver, supporting_signals
from kalshi_predictor.utils.decimals import to_decimal


def explain_opportunity(
    ranking: Any | None,
    *,
    snapshot: Any | None = None,
    forecast: Any | None = None,
    consensus_signal: Any | None = None,
    position_text: str = "No paper position",
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    if ranking is None:
        return _missing_explanation()

    side = _field(ranking, "best_side")
    model_name = str(_field(ranking, "forecast_model") or _field(ranking, "model_name") or "model")
    edge = to_decimal(_field(ranking, "estimated_edge")) or Decimal("0")
    score = to_decimal(_field(ranking, "opportunity_score")) or Decimal("0")
    forecast_probability = _field(ranking, "forecast_probability")
    price = _field(ranking, "best_price")
    risk = explain_risks(
        ranking,
        snapshot,
        fresh_data_minutes=resolved_settings.autopilot_require_fresh_data_minutes,
    )
    freshness = data_freshness(
        snapshot,
        fresh_data_minutes=resolved_settings.autopilot_require_fresh_data_minutes,
    )
    feature_json = _feature_json(forecast)

    recommendation = recommendation_text(side, edge=edge, score=score)
    confidence_label = confidence_from_score(score)
    edge_cents = cents(edge)
    score_label = f"{score.quantize(Decimal('1'))}/100"
    top_reason = opportunity_reason(
        side=side,
        model_name=model_name,
        forecast_probability=forecast_probability,
        price=price,
        edge=edge,
    )
    badges = _badges(
        side=side,
        edge=edge,
        score=score,
        risk_badges=risk["badges"],
        freshness=freshness,
    )
    what_bot_would_do = _what_bot_would_do(side, price)
    recommended_action = _recommended_action(edge=edge, score=score, risk_level=risk["level"])
    consensus = assess_forum_consensus(
        consensus_signal,
        settings=resolved_settings,
        current_price=price,
    )
    why_interesting = top_reason
    if consensus.qualifies:
        why_interesting = f"{top_reason} {consensus.summary}"
        badges.append(consensus.badge or {"label": "Forum Consensus", "kind": "caution"})
        recommended_action = (
            "Review the model edge and the forum-consensus longshot signal together before "
            "running any paper-only cycle."
        )

    return {
        "recommendation": recommendation,
        "confidence_label": confidence_label,
        "edge_cents": edge_cents,
        "score_label": score_label,
        "top_reason": top_reason,
        "top_risk": risk["top_risk"],
        "badges": _dedupe_badges(badges),
        "why_interesting": why_interesting,
        "why_risky": risk["top_risk"],
        "what_bot_would_do": what_bot_would_do,
        "paper_position": position_text,
        "demo_execution_status": "Demo Only / Dry Run",
        "model_confidence": confidence_label,
        "data_freshness": freshness["text"],
        "recommended_action": recommended_action,
        "model_explanation": explain_model(
            model_name,
            forecast_probability=forecast_probability,
            feature_json=feature_json,
        ),
        "primary_driver": primary_driver(ranking, forecast=forecast),
        "supporting_signals": supporting_signals(ranking, forecast=forecast, snapshot=snapshot),
        "risks": risk["risks"],
        "forum_consensus": assessment_to_dict(consensus),
    }


def recommendation_text(side: Any, *, edge: Decimal, score: Decimal) -> str:
    if not side or edge <= 0 or score < Decimal("40"):
        return "No trade recommended"
    if str(side) == "BUY_YES":
        return "Bot would buy YES"
    if str(side) == "BUY_NO":
        return "Bot would buy NO"
    return "No trade recommended"


def confidence_from_score(score: Decimal) -> str:
    if score >= Decimal("75"):
        return "High"
    if score >= Decimal("50"):
        return "Medium"
    return "Low"


def opportunity_reason(
    *,
    side: Any,
    model_name: str,
    forecast_probability: Any,
    price: Any,
    edge: Decimal,
) -> str:
    side_text = "YES" if side == "BUY_YES" else "NO" if side == "BUY_NO" else "a side"
    probability = _percent(forecast_probability)
    price_text = _price_text(price)
    if probability and price_text:
        return (
            f"This market is ranked because {model_name} estimates the {side_text} "
            f"probability above the current market price by {cents(edge)}."
        )
    return f"This market is ranked because the stored model edge is {cents(edge)}."


def cents(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('0.1'))} cents"


def _badges(
    *,
    side: Any,
    edge: Decimal,
    score: Decimal,
    risk_badges: list[dict[str, str]],
    freshness: dict[str, str],
) -> list[dict[str, str]]:
    badges = [{"label": "Demo Only", "kind": "info"}, {"label": "Dry Run", "kind": "info"}]
    if not side or edge <= 0 or score < Decimal("40"):
        badges.append({"label": "No Trade", "kind": "neutral"})
    badges.extend(risk_badges)
    if freshness["badge"] == "Stale Data":
        badges.append({"label": "Stale Data", "kind": "risk"})
    return _dedupe_badges(badges)


def _what_bot_would_do(side: Any, price: Any) -> str:
    if side == "BUY_YES":
        return f"In dry-run mode, the bot would prepare a demo YES order near {price}."
    if side == "BUY_NO":
        return f"In dry-run mode, the bot would prepare a demo NO order near {price}."
    return "The bot would skip this market until the stored edge improves."


def _recommended_action(*, edge: Decimal, score: Decimal, risk_level: str) -> str:
    if edge <= 0 or score < Decimal("40"):
        return "Do not trade; wait for a better edge or fresher signal."
    if risk_level in {"Risky", "Stale Data"}:
        return "Review the risk details before running any dry-run cycle."
    return "Review the full breakdown, then use demo-only dry-run if it still looks reasonable."


def _missing_explanation() -> dict[str, Any]:
    return {
        "recommendation": "No trade recommended",
        "confidence_label": "Low",
        "edge_cents": "n/a",
        "score_label": "n/a",
        "top_reason": "No ranked opportunity is available yet.",
        "top_risk": "Run forecasts and opportunity scanning before reviewing this market.",
        "badges": [{"label": "No Trade", "kind": "neutral"}],
        "why_interesting": "No local ranking exists for this market yet.",
        "why_risky": "There is not enough stored data to explain a trade.",
        "what_bot_would_do": "The bot would skip this market.",
        "paper_position": "No paper position",
        "demo_execution_status": "Demo Only / Dry Run",
        "model_confidence": "Low",
        "data_freshness": "No snapshot is available.",
        "recommended_action": "Run collection, forecasting, and opportunity scanning first.",
        "model_explanation": "No model explanation is available without a forecast.",
        "primary_driver": "No ranked opportunity yet.",
        "supporting_signals": ["Run collection, forecasting, and opportunity scanning first."],
        "risks": ["Missing local opportunity data."],
        "forum_consensus": assessment_to_dict(
            assess_forum_consensus(None, settings=get_settings())
        ),
    }


def _feature_json(forecast: Any | None) -> dict[str, Any]:
    if forecast is None:
        return {}
    value = _field(forecast, "feature_json")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _percent(value: Any) -> str | None:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return None
    return f"{(decimal_value * Decimal('100')).quantize(Decimal('0.1'))}%"


def _price_text(value: Any) -> str | None:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return None
    return f"{(decimal_value * Decimal('100')).quantize(Decimal('0.1'))} cents"


def _dedupe_badges(badges: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for badge in badges:
        if badge["label"] in seen:
            continue
        seen.add(badge["label"])
        unique.append(badge)
    return unique
