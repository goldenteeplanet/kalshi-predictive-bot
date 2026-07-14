from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    OpportunityResearchSnapshot,
    ResearchNote,
    ResearchQuestion,
)
from kalshi_predictor.utils.time import utc_now


def store_research_note(
    session: Session,
    *,
    evidence: dict[str, Any],
    narrative: dict[str, Any],
    note_type: str = "opportunity_research",
) -> ResearchNote:
    note = ResearchNote(
        created_at=utc_now(),
        ticker=str(evidence.get("ticker") or ""),
        model_name=str(evidence.get("model_name") or ""),
        note_type=note_type,
        title=str(evidence.get("short_market_name") or evidence.get("ticker") or "Research note"),
        summary=str(narrative.get("summary") or narrative.get("why_ranked") or ""),
        evidence_json=encode_json(evidence),
        risks_json=encode_json(narrative.get("risks") or []),
        recommendation=str(narrative.get("recommendation") or ""),
        confidence_label=str(narrative.get("confidence_label") or "Unknown"),
        raw_json=encode_json({"evidence": evidence, "narrative": narrative}),
    )
    session.add(note)
    return note


def store_research_question(
    session: Session,
    *,
    result: dict[str, Any],
) -> ResearchQuestion:
    row = ResearchQuestion(
        created_at=utc_now(),
        question=str(result.get("question") or ""),
        ticker=result.get("ticker"),
        model_name=result.get("model_name"),
        answer=str(result.get("answer") or ""),
        evidence_json=encode_json(result.get("evidence") or {}),
        raw_json=encode_json(result),
    )
    session.add(row)
    return row


def store_opportunity_research_snapshot(
    session: Session,
    *,
    evidence: dict[str, Any],
    narrative: dict[str, Any],
) -> OpportunityResearchSnapshot:
    row = OpportunityResearchSnapshot(
        created_at=utc_now(),
        ticker=str(evidence.get("ticker") or ""),
        model_name=str(evidence.get("model_name") or ""),
        rank=evidence.get("rank"),
        opportunity_score=evidence.get("opportunity_score"),
        edge=evidence.get("edge"),
        market_price=evidence.get("market_price"),
        model_probability=evidence.get("model_probability"),
        primary_driver=str(narrative.get("primary_driver") or evidence.get("primary_signal") or ""),
        supporting_signals_json=encode_json(evidence.get("supporting_signals") or []),
        risk_factors_json=encode_json(evidence.get("risk_factors") or []),
        recommendation=str(narrative.get("recommendation") or ""),
        raw_json=encode_json({"evidence": evidence, "narrative": narrative}),
    )
    session.add(row)
    return row


def recent_research_snapshots(
    session: Session,
    *,
    ticker: str,
    model_name: str,
    limit: int = 2,
) -> list[OpportunityResearchSnapshot]:
    return list(
        session.scalars(
            select(OpportunityResearchSnapshot)
            .where(
                OpportunityResearchSnapshot.ticker == ticker,
                OpportunityResearchSnapshot.model_name == model_name,
            )
            .order_by(
                desc(OpportunityResearchSnapshot.created_at),
                desc(OpportunityResearchSnapshot.id),
            )
            .limit(limit)
        )
    )


def compare_latest_research_snapshots(
    session: Session,
    *,
    ticker: str,
    model_name: str,
) -> dict[str, Any]:
    rows = recent_research_snapshots(
        session,
        ticker=ticker,
        model_name=model_name,
        limit=2,
    )
    if len(rows) < 2:
        return {
            "available": False,
            "summary": "No prior research snapshot is available yet.",
        }
    current, previous = rows[0], rows[1]
    changes = {
        "available": True,
        "rank_change": _delta(current.rank, previous.rank),
        "score_change": _decimal_delta(current.opportunity_score, previous.opportunity_score),
        "edge_change": _decimal_delta(current.edge, previous.edge),
        "recommendation_changed": current.recommendation != previous.recommendation,
        "summary": "",
    }
    changes["summary"] = (
        f"Rank change {changes['rank_change']}; score change {changes['score_change']}; "
        f"edge change {changes['edge_change']}; recommendation changed "
        f"{changes['recommendation_changed']}."
    )
    return changes


def _delta(current: int | None, previous: int | None) -> str:
    if current is None or previous is None:
        return "n/a"
    return str(current - previous)


def _decimal_delta(current: str | None, previous: str | None) -> str:
    if current is None or previous is None:
        return "n/a"
    try:
        return str(float(current) - float(previous))
    except ValueError:
        return "n/a"

