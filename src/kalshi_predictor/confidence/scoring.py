from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.decimals import to_decimal

LABEL_LEADER = "Leader"
LABEL_PROMISING = "Promising"
LABEL_NEEDS_DATA = "Needs More Data"
LABEL_UNDERPERFORMING = "Underperforming"


def score_model_confidence_metrics(
    row: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    settled = int(row.get("settled_trade_count") or 0)
    sample_score = _sample_size_score(settled, resolved_settings)
    calibration_score = _calibration_score(row.get("brier_score"))
    profitability_score = _profitability_score(row.get("roi_on_exposure"))
    drawdown_score = _drawdown_score(row.get("max_drawdown"))
    confidence_score = (
        sample_score * Decimal("0.25")
        + calibration_score * Decimal("0.30")
        + profitability_score * Decimal("0.30")
        + drawdown_score * Decimal("0.15")
    ).quantize(Decimal("0.0001"))
    label = _label(row, settled, confidence_score, resolved_settings)
    status = "OK" if label in {LABEL_LEADER, LABEL_PROMISING} else label.upper().replace(" ", "_")
    notes = _notes(label, settled, resolved_settings)
    scored = dict(row)
    scored.update(
        {
            "sample_size_score": sample_score.quantize(Decimal("0.0001")),
            "calibration_score": calibration_score.quantize(Decimal("0.0001")),
            "profitability_score": profitability_score.quantize(Decimal("0.0001")),
            "drawdown_score": drawdown_score.quantize(Decimal("0.0001")),
            "confidence_score": confidence_score,
            "confidence_label": label,
            "status": status,
            "notes": notes,
        }
    )
    return scored


def promote_category_leaders(
    rows: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
) -> None:
    resolved_settings = settings or get_settings()
    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_category.setdefault(str(row.get("category") or "general"), []).append(row)
    for category_rows in by_category.values():
        eligible = [
            row
            for row in category_rows
            if int(row.get("settled_trade_count") or 0)
            >= resolved_settings.model_confidence_min_settled_trades
            and row.get("confidence_label") != LABEL_UNDERPERFORMING
        ]
        if not eligible:
            continue
        best = max(
            eligible,
            key=lambda row: to_decimal(row.get("confidence_score")) or Decimal("0"),
        )
        best["confidence_label"] = LABEL_LEADER
        best["status"] = "OK"
        best["notes"] = "Current category leader from settled paper and forecast results."


def _sample_size_score(settled: int, settings: Settings) -> Decimal:
    target = max(1, settings.model_confidence_min_settled_trades)
    return min(Decimal("100"), Decimal(settled) / Decimal(target) * Decimal("100"))


def _calibration_score(brier_value: Any) -> Decimal:
    brier = to_decimal(brier_value)
    if brier is None:
        return Decimal("35")
    return max(Decimal("0"), min(Decimal("100"), Decimal("100") - brier * Decimal("250")))


def _profitability_score(roi_value: Any) -> Decimal:
    roi = to_decimal(roi_value)
    if roi is None:
        return Decimal("35")
    return max(Decimal("0"), min(Decimal("100"), Decimal("50") + roi * Decimal("100")))


def _drawdown_score(drawdown_value: Any) -> Decimal:
    drawdown = abs(to_decimal(drawdown_value) or Decimal("0"))
    return max(Decimal("0"), min(Decimal("100"), Decimal("100") - drawdown * Decimal("25")))


def _label(
    row: dict[str, Any],
    settled: int,
    confidence_score: Decimal,
    settings: Settings,
) -> str:
    if settled < settings.model_confidence_min_settled_trades:
        return LABEL_NEEDS_DATA
    roi = to_decimal(row.get("roi_on_exposure"))
    brier = to_decimal(row.get("brier_score"))
    if (roi is not None and roi < 0) or (brier is not None and brier > Decimal("0.35")):
        return LABEL_UNDERPERFORMING
    if confidence_score >= Decimal("55"):
        return LABEL_PROMISING
    return LABEL_UNDERPERFORMING


def _notes(label: str, settled: int, settings: Settings) -> str:
    if label == LABEL_NEEDS_DATA:
        return (
            f"Only {settled} settled trades; needs "
            f"{settings.model_confidence_min_settled_trades} before full weighting."
        )
    if label == LABEL_UNDERPERFORMING:
        return "Calibration or paper P&L is weak; down-weight until it improves."
    return "Settled outcomes support continued use."
