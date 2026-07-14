from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.research.evidence import (
    build_opportunity_evidence,
    current_rankings,
    latest_forecast,
    top_opportunity_evidence,
)
from kalshi_predictor.research.narratives import generate_narrative
from kalshi_predictor.research.repository import compare_latest_research_snapshots
from kalshi_predictor.ui.market_display import format_probability
from kalshi_predictor.utils.decimals import to_decimal

SUPPORTED_QUESTIONS = [
    "Why is this ranked #1?",
    "Why does the bot like this?",
    "Why is this risky?",
    "What is the main driver?",
    "What changed since last run?",
    "Should this be paper traded?",
    "Should this be demo dry-run only?",
    "What data is missing?",
    "Which model is driving this?",
    "How does this compare to market_implied_v1?",
    "Why did the bot skip this?",
    "What are the top 5 opportunities and why?",
]


def answer_research_question(
    session: Session,
    *,
    question: str,
    ticker: str | None = None,
    model_name: str = "ensemble_v2",
) -> dict[str, Any]:
    normalized = _normalize(question)
    evidence = _evidence_for_question(
        session,
        question=normalized,
        ticker=ticker,
        model_name=model_name,
    )
    narrative = generate_narrative(evidence)

    if "top 5" in normalized:
        answer = _top_five_answer(session, model_name=model_name)
        evidence_payload: Any = top_opportunity_evidence(session, model_name=model_name, limit=5)
    elif "changed since last run" in normalized:
        answer = _changed_answer(session, evidence)
        evidence_payload = evidence
    elif "data is missing" in normalized:
        missing = evidence.get("missing_data") or []
        answer = (
            "No major data gaps were detected."
            if not missing
            else "Missing or weak data: " + ", ".join(missing) + "."
        )
        evidence_payload = evidence
    elif "main driver" in normalized:
        answer = str(narrative["primary_driver"])
        evidence_payload = evidence
    elif "risky" in normalized:
        answer = " ".join(evidence.get("risk_factors") or narrative.get("risks") or [])
        evidence_payload = evidence
    elif "paper traded" in normalized:
        answer = _paper_trade_answer(evidence)
        evidence_payload = evidence
    elif "demo dry-run" in normalized:
        answer = _demo_dry_run_answer(evidence)
        evidence_payload = evidence
    elif "which model" in normalized or "model is driving" in normalized:
        answer = _model_driver_answer(evidence)
        evidence_payload = evidence
    elif "market_implied_v1" in normalized:
        answer = _market_implied_comparison(session, evidence)
        evidence_payload = evidence
    elif "skip" in normalized:
        answer = _skip_answer(evidence)
        evidence_payload = evidence
    elif "ranked" in normalized:
        answer = narrative["why_ranked"]
        evidence_payload = evidence
    else:
        answer = narrative["bot_thinks"]
        evidence_payload = evidence

    return {
        "question": question,
        "ticker": evidence.get("ticker"),
        "model_name": model_name,
        "answer": answer,
        "evidence": evidence_payload,
        "raw": {
            "normalized_question": normalized,
            "supported_questions": SUPPORTED_QUESTIONS,
        },
    }


def _evidence_for_question(
    session: Session,
    *,
    question: str,
    ticker: str | None,
    model_name: str,
) -> dict[str, Any]:
    resolved_ticker = ticker
    if resolved_ticker is None and ("ranked #1" in question or "top 5" not in question):
        rankings = current_rankings(session, model_name=model_name, limit=1)
        if rankings:
            resolved_ticker = rankings[0].ticker
    if resolved_ticker is None:
        top = top_opportunity_evidence(session, model_name=model_name, limit=1)
        if top:
            return top[0]
        return build_opportunity_evidence(session, ticker="", model_name=model_name)
    return build_opportunity_evidence(session, ticker=resolved_ticker, model_name=model_name)


def _top_five_answer(session: Session, *, model_name: str) -> str:
    rows = top_opportunity_evidence(session, model_name=model_name, limit=5)
    if not rows:
        return "No ranked opportunities are available yet."
    parts = []
    for row in rows:
        narrative = generate_narrative(row)
        parts.append(
            f"{row.get('rank') or '?'}: {row['ticker']} - "
            f"{narrative['primary_driver']} {narrative['recommendation']}"
        )
    return "\n".join(parts)


def _changed_answer(session: Session, evidence: dict[str, Any]) -> str:
    ticker = str(evidence.get("ticker") or "")
    model_name = str(evidence.get("model_name") or "")
    if not ticker or not model_name:
        return "No current ticker/model context is available for change comparison."
    return str(
        compare_latest_research_snapshots(
            session,
            ticker=ticker,
            model_name=model_name,
        )["summary"]
    )


def _paper_trade_answer(evidence: dict[str, Any]) -> str:
    score = to_decimal(evidence.get("opportunity_score")) or Decimal("0")
    edge = to_decimal(evidence.get("edge")) or Decimal("0")
    risks = " ".join(evidence.get("risk_factors") or [])
    if score >= Decimal("70") and edge > 0 and "No latest forecast" not in risks:
        return "Paper trade only after refreshing data; this is not a live-trading signal."
    return "Do not paper trade yet. The edge, score, or evidence quality is not strong enough."


def _demo_dry_run_answer(evidence: dict[str, Any]) -> str:
    score = to_decimal(evidence.get("opportunity_score")) or Decimal("0")
    edge = to_decimal(evidence.get("edge")) or Decimal("0")
    if score >= Decimal("80") and edge >= Decimal("0.05"):
        return "A demo dry-run is reasonable for review only. Do not place live trades."
    return "Hold off on demo dry-runs until the score and edge are stronger."


def _model_driver_answer(evidence: dict[str, Any]) -> str:
    components = evidence.get("component_models") or {}
    if not components:
        return f"{evidence.get('model_name')} is the active model; component details are missing."
    parts = [f"{name}: {format_probability(value)}" for name, value in components.items()]
    return f"{evidence.get('model_name')} is driven by " + ", ".join(parts) + "."


def _market_implied_comparison(session: Session, evidence: dict[str, Any]) -> str:
    ticker = str(evidence.get("ticker") or "")
    if not ticker:
        return "No ticker context is available for comparison."
    implied = latest_forecast(session, ticker=ticker, model_name="market_implied_v1")
    if implied is None:
        return "No market_implied_v1 forecast is available for this ticker yet."
    current_probability = format_probability(evidence.get("model_probability"))
    implied_probability = format_probability(implied.yes_probability)
    return (
        f"{evidence.get('model_name')} is at {current_probability}; "
        f"market_implied_v1 is at {implied_probability}."
    )


def _skip_answer(evidence: dict[str, Any]) -> str:
    score = to_decimal(evidence.get("opportunity_score")) or Decimal("0")
    edge = to_decimal(evidence.get("edge")) or Decimal("0")
    if score < Decimal("60"):
        return "The bot skipped or downgraded this because the opportunity score is below 60."
    if edge <= 0:
        return "The bot skipped or downgraded this because the estimated edge is not positive."
    return "The bot has not clearly skipped this; review risks and freshness before acting."


def _normalize(question: str) -> str:
    return " ".join(question.strip().lower().split())

