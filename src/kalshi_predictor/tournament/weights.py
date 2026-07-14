from collections import defaultdict
from decimal import Decimal
from typing import Any

from kalshi_predictor.tournament.ranking import CATEGORIES
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


def generate_model_weights(
    rows: list[dict[str, Any]],
    *,
    lookback_days: int,
) -> list[dict[str, Any]]:
    generated_at = utc_now()
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_category[str(row["category"])].append(row)

    weights: list[dict[str, Any]] = []
    for category in sorted(set(CATEGORIES) | set(by_category)):
        category_rows = by_category.get(category, [])
        scored = [(row, _weight_score(row)) for row in category_rows]
        positive = [(row, score) for row, score in scored if score > 0]
        if not positive:
            weights.append(
                _weight_row(
                    generated_at=generated_at,
                    model_name="market_implied_v1",
                    category=category,
                    weight=Decimal("1.0"),
                    lookback_days=lookback_days,
                    method="fallback_market_implied",
                    notes="No model had enough tournament data; using market_implied_v1.",
                )
            )
            continue
        total = sum((score for _, score in positive), Decimal("0"))
        assigned = Decimal("0")
        for index, (row, score) in enumerate(positive):
            normalized = score / total if total else Decimal("0")
            if index == len(positive) - 1:
                normalized = Decimal("1.0") - assigned
            assigned += normalized
            weights.append(
                _weight_row(
                    generated_at=generated_at,
                    model_name=str(row["model_name"]),
                    category=category,
                    weight=normalized,
                    lookback_days=lookback_days,
                    method="tournament_v1",
                    notes="Weight from calibration, ROI, and sample-size tournament score.",
                )
            )
    return weights


def _weight_score(row: dict[str, Any]) -> Decimal:
    if row.get("status") == "INSUFFICIENT_DATA":
        return Decimal("0")
    brier = to_decimal(row.get("brier_score"))
    roi = to_decimal(row.get("roi_on_exposure")) or Decimal("0")
    evaluated = Decimal(int(row.get("evaluated_forecast_count") or 0))
    if brier is None:
        return Decimal("0")
    calibration = Decimal("1") / (Decimal("1") + brier)
    sample = min(evaluated / Decimal("100"), Decimal("1"))
    roi_multiplier = Decimal("0.25") if roi < 0 else Decimal("1") + min(roi, Decimal("1"))
    if brier > Decimal("0.35"):
        calibration *= Decimal("0.5")
    return calibration * sample * roi_multiplier


def _weight_row(
    *,
    generated_at: Any,
    model_name: str,
    category: str,
    weight: Decimal,
    lookback_days: int,
    method: str,
    notes: str,
) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "model_name": model_name,
        "category": category,
        "weight": decimal_to_str(weight) or "0",
        "method": method,
        "lookback_days": lookback_days,
        "notes": notes,
        "raw_json": {
            "model_name": model_name,
            "category": category,
            "weight": decimal_to_str(weight) or "0",
            "method": method,
            "notes": notes,
        },
    }
