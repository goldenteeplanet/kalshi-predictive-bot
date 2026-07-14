from collections import defaultdict
from typing import Any

from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def generate_model_diagnostics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    generated_at = utc_now()
    diagnostics: list[dict[str, Any]] = []
    categories_by_model: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        categories_by_model[str(row["model_name"])].add(str(row["category"]))

    for row in rows:
        diagnostics.extend(_row_diagnostics(row, generated_at))

    for model_name, categories in categories_by_model.items():
        diagnostics.append(
            {
                "generated_at": generated_at,
                "model_name": model_name,
                "category": ",".join(sorted(categories)),
                "diagnostic_type": "category_coverage",
                "metric_name": "category_count",
                "metric_value": len(categories),
                "notes": _coverage_note(model_name, categories),
                "raw_json": {"categories": sorted(categories)},
            }
        )
    return diagnostics


def _row_diagnostics(row: dict[str, Any], generated_at: Any) -> list[dict[str, Any]]:
    forecast_count = int(row.get("forecast_count") or 0)
    evaluated = int(row.get("evaluated_forecast_count") or 0)
    total_pnl = to_decimal(row.get("total_pnl"))
    roi = to_decimal(row.get("roi_on_exposure"))
    brier = to_decimal(row.get("brier_score"))
    max_drawdown = abs(to_decimal(row.get("max_drawdown")) or 0)
    diagnostics = [
        _diagnostic(
            generated_at,
            row,
            "sample_size",
            "evaluated_forecast_count",
            evaluated,
            "Not enough settled forecasts" if evaluated < 5 else "Settled sample size is usable",
        ),
        _diagnostic(
            generated_at,
            row,
            "skipped_forecasts",
            "unevaluated_forecast_count",
            max(forecast_count - evaluated, 0),
            (
                "Some forecasts lack settlements"
                if forecast_count > evaluated
                else "All forecasts in this row are evaluated"
            ),
        ),
    ]
    if brier is None:
        diagnostics.append(
            _diagnostic(
                generated_at,
                row,
                "calibration",
                "brier_score",
                None,
                "No calibration data",
            )
        )
    elif brier > 0.30:
        diagnostics.append(
            _diagnostic(
                generated_at,
                row,
                "overconfidence",
                "brier_score",
                brier,
                "Weak calibration; check for overconfidence",
            )
        )
    else:
        diagnostics.append(
            _diagnostic(
                generated_at,
                row,
                "calibration",
                "brier_score",
                brier,
                "Calibration is usable",
            )
        )

    if total_pnl is not None and total_pnl < 0:
        notes = "Negative P&L in simulated trades"
    elif (total_pnl or 0) > 0 and brier is not None and brier > 0.30:
        notes = "Positive P&L but weak calibration"
    elif brier is not None and brier <= 0.25 and (total_pnl is None or total_pnl == 0):
        notes = "Good calibration but no paper-trading edge"
    else:
        notes = "P&L diagnostics are neutral"
    diagnostics.append(_diagnostic(generated_at, row, "pnl", "total_pnl", total_pnl, notes))

    if total_pnl is not None and max_drawdown > abs(total_pnl):
        diagnostics.append(
            _diagnostic(
                generated_at,
                row,
                "pnl",
                "max_drawdown",
                max_drawdown,
                "High drawdown relative to total P&L",
            )
        )
    if roi is not None and roi < 0:
        diagnostics.append(
            _diagnostic(generated_at, row, "pnl", "roi_on_exposure", roi, "Negative simulated ROI")
        )
    return diagnostics


def _diagnostic(
    generated_at: Any,
    row: dict[str, Any],
    diagnostic_type: str,
    metric_name: str,
    metric_value: Any,
    notes: str,
) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "model_name": row["model_name"],
        "category": row["category"],
        "diagnostic_type": diagnostic_type,
        "metric_name": metric_name,
        "metric_value": metric_value,
        "notes": notes,
        "raw_json": {
            "model_name": row["model_name"],
            "category": row["category"],
            "status": row.get("status"),
        },
    }


def _coverage_note(model_name: str, categories: set[str]) -> str:
    if categories == {"crypto"}:
        return "Model only active in crypto markets"
    if categories == {"weather"}:
        return "Model only active in weather markets"
    if len(categories) == 1:
        return f"Model only active in {next(iter(categories))} markets"
    return f"{model_name} spans {len(categories)} categories"
