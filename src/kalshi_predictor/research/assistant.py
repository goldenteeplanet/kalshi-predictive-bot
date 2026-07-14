from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.meta.explanations import explain_meta_selection
from kalshi_predictor.research.evidence import (
    build_opportunity_evidence,
    top_opportunity_evidence,
)
from kalshi_predictor.research.narratives import generate_narrative
from kalshi_predictor.research.questions import SUPPORTED_QUESTIONS
from kalshi_predictor.research.repository import store_research_note


def research_opportunity(
    session: Session,
    *,
    ticker: str,
    model_name: str = "ensemble_v2",
    persist_note: bool = False,
) -> dict[str, Any]:
    evidence = build_opportunity_evidence(session, ticker=ticker, model_name=model_name)
    narrative = generate_narrative(evidence)
    meta_selection = explain_meta_selection(session, ticker)
    note = None
    if persist_note:
        note = store_research_note(session, evidence=evidence, narrative=narrative)
    return {
        "evidence": evidence,
        "narrative": narrative,
        "meta_selection": meta_selection,
        "note": note,
    }


def research_dashboard(
    session: Session,
    *,
    model_name: str = "ensemble_v2",
    limit: int = 5,
) -> dict[str, Any]:
    items = []
    top_risks: list[str] = []
    missing_warnings: list[str] = []
    model_drivers: list[str] = []
    for evidence in top_opportunity_evidence(session, model_name=model_name, limit=limit):
        narrative = generate_narrative(evidence)
        items.append({"evidence": evidence, "narrative": narrative})
        top_risks.extend(evidence.get("risk_factors") or [])
        missing_warnings.extend(evidence.get("missing_data") or [])
        driver = narrative.get("primary_driver")
        if driver:
            model_drivers.append(str(driver))
    return {
        "model_name": model_name,
        "questions": SUPPORTED_QUESTIONS,
        "top_opportunities": items,
        "top_risks": _unique(top_risks)[:8],
        "missing_warnings": _unique(missing_warnings)[:8],
        "model_drivers": _unique(model_drivers)[:8],
    }


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
