from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.meta.diagnostics import meta_diagnostics
from kalshi_predictor.meta.repository import latest_meta_decision, row_to_dict


def explain_meta_selection(session: Session, ticker: str) -> dict[str, Any]:
    decision = row_to_dict(latest_meta_decision(session, ticker))
    if decision is None:
        return {
            "available": False,
            "selected_model": "n/a",
            "trust_score": "n/a",
            "fallback": False,
            "summary": (
                "No meta model decision exists yet. Run build-meta-features and "
                "forecast meta_model_v1 first."
            ),
            "diagnostics": [],
        }
    fallback = bool(decision.get("fallback_model_name"))
    selected = str(decision.get("selected_model_name") or "n/a")
    trust = decision.get("selected_confidence") or "n/a"
    reason = decision.get("decision_reason") or "No reason stored."
    if fallback:
        summary = (
            f"Falling back to {decision['fallback_model_name']} because the meta selector "
            "did not find enough trustworthy specialized evidence."
        )
    else:
        summary = f"{selected} was selected because {reason}"
    ticker_diagnostics = [
        row for row in meta_diagnostics(session) if str(ticker) in row.get("message", "")
    ]
    return {
        "available": True,
        "selected_model": selected,
        "trust_score": trust,
        "fallback": fallback,
        "summary": summary,
        "decision": decision,
        "diagnostics": ticker_diagnostics,
    }
