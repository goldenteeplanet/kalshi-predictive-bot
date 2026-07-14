from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import to_decimal


def generate_narrative(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("found"):
        return _missing_opportunity_narrative(evidence)

    score = to_decimal(evidence.get("opportunity_score")) or Decimal("0")
    edge = to_decimal(evidence.get("edge")) or Decimal("0")
    rank_label = f"#{evidence['rank']}" if evidence.get("rank") else "unranked"
    recommendation = _recommendation(evidence, score=score, edge=edge)
    confidence_label = _confidence_label(evidence, score=score)

    why_ranked = (
        f"{evidence['short_market_name']} is ranked {rank_label} for "
        f"{evidence['model_name']} because the model estimates "
        f"{evidence.get('model_probability_label') or 'an unknown probability'} while "
        f"the current market price is {evidence.get('market_price') or 'unknown'}."
    )
    if evidence.get("edge_cents") != "n/a":
        why_ranked += f" That creates an estimated edge of {evidence['edge_cents']}."
    if score < Decimal("60"):
        why_ranked += " This is not a strong opportunity yet."

    supporting = evidence.get("supporting_signals") or []
    risks = evidence.get("risk_factors") or []
    missing = evidence.get("missing_data") or []
    primary_driver = evidence.get("primary_signal") or "No primary driver is available yet."

    bot_thinks = (
        f"The bot currently reads this as {evidence.get('side_label') or 'a watchlist item'}. "
        f"Primary driver: {primary_driver}"
    )
    if evidence.get("expected_value"):
        bot_thinks += f" Expected value is {evidence['expected_value']}."

    next_action = _next_action(evidence, score=score, edge=edge, risks=risks)
    missing_text = (
        "No major data gaps were detected."
        if not missing
        else "Missing or weak data: " + ", ".join(missing) + "."
    )
    strength = _evidence_strength(evidence, score=score, missing=missing, risks=risks)

    return {
        "summary": f"{rank_label} {evidence['short_market_name']}: {recommendation}",
        "why_ranked": why_ranked,
        "bot_thinks": bot_thinks,
        "supporting_signals": supporting,
        "risks": risks,
        "evidence_strength": strength,
        "next_action": next_action,
        "missing_data": missing_text,
        "primary_driver": primary_driver,
        "recommendation": recommendation,
        "confidence_label": confidence_label,
        "sections": [
            {"title": "Why this is ranked here", "body": why_ranked},
            {"title": "What the bot thinks", "body": bot_thinks},
            {"title": "What signals support it", "items": supporting},
            {"title": "What could go wrong", "items": risks},
            {"title": "How strong the evidence is", "body": strength},
            {"title": "What to do next", "body": next_action},
            {"title": "What data is missing", "body": missing_text},
        ],
    }


def render_research_markdown(evidence: dict[str, Any], narrative: dict[str, Any]) -> str:
    lines = [
        f"## {evidence.get('short_market_name') or evidence.get('ticker')}",
        "",
        f"- Ticker: `{evidence.get('ticker')}`",
        f"- Model: `{evidence.get('model_name')}`",
        f"- Recommendation: {narrative['recommendation']}",
        f"- Confidence: {narrative['confidence_label']}",
        "",
        "### Why this is ranked here",
        "",
        narrative["why_ranked"],
        "",
        "### What the bot thinks",
        "",
        narrative["bot_thinks"],
        "",
        "### Supporting signals",
        "",
    ]
    lines.extend(f"- {signal}" for signal in narrative.get("supporting_signals", []))
    lines.extend(["", "### Risks", ""])
    lines.extend(f"- {risk}" for risk in narrative.get("risks", []))
    lines.extend(
        [
            "",
            "### Evidence strength",
            "",
            narrative["evidence_strength"],
            "",
            "### Next action",
            "",
            narrative["next_action"],
            "",
            "### Missing data",
            "",
            narrative["missing_data"],
            "",
        ]
    )
    return "\n".join(lines)


def _missing_opportunity_narrative(evidence: dict[str, Any]) -> dict[str, Any]:
    ticker = evidence.get("ticker") or "unknown"
    body = (
        f"No local research writeup can be completed for {ticker} yet because the bot "
        "does not have enough stored ranking, forecast, or market data."
    )
    missing = evidence.get("missing_data") or ["opportunity ranking", "latest forecast"]
    return {
        "summary": body,
        "why_ranked": body,
        "bot_thinks": "The bot should not treat this as an opportunity yet.",
        "supporting_signals": ["No supporting signals are available yet."],
        "risks": ["Missing local evidence."],
        "evidence_strength": "Weak. The evidence set is incomplete.",
        "next_action": "Collect data, run forecasts, and regenerate opportunities before review.",
        "missing_data": "Missing or weak data: " + ", ".join(missing) + ".",
        "primary_driver": "No primary driver is available yet.",
        "recommendation": "Do not paper trade yet.",
        "confidence_label": "Weak",
        "sections": [],
    }


def _recommendation(evidence: dict[str, Any], *, score: Decimal, edge: Decimal) -> str:
    risks = evidence.get("risk_factors") or []
    if score >= Decimal("80") and edge >= Decimal("0.05") and not _has_blocking_risk(risks):
        return "Keep on the watchlist or run a demo dry-run only."
    if score >= Decimal("65") and edge > 0:
        return "Watchlist this and paper trade only if fresh data still confirms the edge."
    return "This is not a strong opportunity yet; keep collecting evidence."


def _confidence_label(evidence: dict[str, Any], *, score: Decimal) -> str:
    missing = evidence.get("missing_data") or []
    if score >= Decimal("80") and len(missing) <= 1:
        return "Strong"
    if score >= Decimal("60") and len(missing) <= 3:
        return "Moderate"
    return "Weak"


def _evidence_strength(
    evidence: dict[str, Any],
    *,
    score: Decimal,
    missing: list[str],
    risks: list[str],
) -> str:
    if score >= Decimal("80") and not missing:
        return "Strong. Ranking, forecast, and market data are present."
    if score >= Decimal("60") and len(missing) <= 3:
        return "Moderate. The opportunity has useful evidence, but some inputs need review."
    if any("Not enough backtest history yet" in risk for risk in risks):
        return "Weak to moderate. Not enough backtest history yet."
    return "Weak. The local evidence set is incomplete or the opportunity score is low."


def _next_action(
    evidence: dict[str, Any],
    *,
    score: Decimal,
    edge: Decimal,
    risks: list[str],
) -> str:
    if score >= Decimal("80") and edge >= Decimal("0.05") and not _has_blocking_risk(risks):
        return "Run a demo dry-run or keep this on the paper watchlist. Do not place live trades."
    if score >= Decimal("60"):
        return "Keep this in paper review and refresh forecasts before any demo dry-run."
    return "Do not paper trade yet. Gather fresher data and wait for a clearer edge."


def _has_blocking_risk(risks: list[str]) -> bool:
    blocking_terms = ("Low liquidity", "Wide spread", "stale", "No latest forecast")
    return any(any(term in risk for term in blocking_terms) for risk in risks)
