from collections import Counter
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.meta.repository import (
    latest_meta_performance,
    recent_meta_decisions,
    row_to_dict,
)
from kalshi_predictor.utils.decimals import to_decimal


def meta_diagnostics(session: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    decisions = [row_to_dict(row) or {} for row in recent_meta_decisions(session, limit=limit)]
    diagnostics: list[dict[str, Any]] = []
    diagnostics.extend(_decision_diagnostics(decisions))
    diagnostics.extend(_distribution_diagnostics(decisions))
    performance = row_to_dict(latest_meta_performance(session))
    if performance is not None:
        diagnostics.extend(_performance_diagnostics(performance))
    if not diagnostics:
        diagnostics.append(
            {
                "severity": "INFO",
                "title": "Needs meta data",
                "message": "Build meta features and forecasts before relying on meta diagnostics.",
            }
        )
    return diagnostics


def _decision_diagnostics(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for decision in decisions:
        raw = decision.get("raw_json") or {}
        confidence = to_decimal(decision.get("selected_confidence")) or Decimal("0")
        disagreement = to_decimal(raw.get("model_disagreement_score")) or Decimal("0")
        if confidence < Decimal("45"):
            rows.append(
                _row(
                    "WARN",
                    "Selected model has insufficient data",
                    f"{decision.get('ticker')} selected {decision.get('selected_model_name')} "
                    f"with trust {confidence}/100.",
                )
            )
        if decision.get("fallback_model_name"):
            rows.append(
                _row(
                    "INFO",
                    "Fallback was used",
                    f"{decision.get('ticker')} fell back to {decision.get('fallback_model_name')}.",
                )
            )
        if disagreement >= Decimal("0.20"):
            rows.append(
                _row(
                    "WARN",
                    "High model disagreement",
                    f"{decision.get('ticker')} has disagreement {disagreement}; review manually.",
                )
            )
    return rows


def _distribution_diagnostics(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not decisions:
        return []
    counts = Counter(str(row.get("selected_model_name") or "unknown") for row in decisions)
    total = sum(counts.values())
    rows: list[dict[str, Any]] = []
    selected, selected_count = counts.most_common(1)[0]
    if Decimal(selected_count) / Decimal(total) > Decimal("0.80") and total >= 5:
        rows.append(
            _row(
                "WARN",
                "Model selected too often",
                f"{selected} accounts for {selected_count}/{total} recent meta decisions.",
            )
        )
    fallback_count = sum(1 for row in decisions if row.get("fallback_model_name"))
    if fallback_count and Decimal(fallback_count) / Decimal(total) > Decimal("0.35"):
        rows.append(
            _row(
                "WARN",
                "Fallback overuse",
                f"{fallback_count}/{total} recent decisions used fallback logic.",
            )
        )
    return rows


def _performance_diagnostics(performance: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    meta_brier = to_decimal(performance.get("meta_brier_score"))
    ensemble_brier = to_decimal(performance.get("ensemble_brier_score"))
    if meta_brier is None:
        rows.append(
            _row(
                "INFO",
                "Meta model needs settled outcomes",
                "No settled meta_model_v1 forecast evaluations are available yet.",
            )
        )
    elif ensemble_brier is not None and meta_brier >= ensemble_brier:
        rows.append(
            _row(
                "WARN",
                "Meta model not beating ensemble_v2",
                f"Meta Brier {meta_brier} is not below ensemble_v2 Brier {ensemble_brier}.",
            )
        )
    meta_roi = to_decimal(performance.get("meta_roi"))
    if meta_roi is not None and meta_roi < 0:
        rows.append(
            _row(
                "WARN",
                "Meta paper ROI is negative",
                f"meta_model_v1 paper ROI is {meta_roi}; keep this paper-only.",
            )
        )
    return rows


def _row(severity: str, title: str, message: str) -> dict[str, str]:
    return {"severity": severity, "title": title, "message": message}
