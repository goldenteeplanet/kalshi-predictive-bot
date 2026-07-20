from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.counterfactual import BASELINE_FORECASTS
from kalshi_predictor.benchmarking.interaction_boundary import (
    FORECAST_BIAS_GRID,
    SPREAD_ADDITION_GRID,
    build_interaction_boundary_refinement,
)
from kalshi_predictor.benchmarking.liquidity_boundary import _evaluate
from kalshi_predictor.benchmarking.oos_policy import CATEGORY_TICKER, _episode_row, _metrics


BASE_SPREAD = Decimal("0.02")
BASE_DEPTH = Decimal("25")


def build_stress_aware_allocation_guard_preview() -> dict[str, Any]:
    boundary = build_interaction_boundary_refinement()
    buffer = boundary["deterministic_safety_buffer"]
    max_bias = Decimal(buffer["maximum_forecast_bias_magnitude"])
    max_spread = Decimal(buffer["maximum_spread_addition"])
    baseline_rows: list[dict[str, Any]] = []
    guarded_rows: list[dict[str, Any]] = []
    index = 0
    for category, ticker in sorted(CATEGORY_TICKER.items()):
        for forecast_bias in FORECAST_BIAS_GRID:
            for spread_addition in SPREAD_ADDITION_GRID:
                forecast = BASELINE_FORECASTS[ticker] + forecast_bias
                scenario = _evaluate(
                    ticker, BASE_SPREAD + spread_addition, BASE_DEPTH, forecast
                )
                settlement = _settlement(category, index)
                baseline_allocate = scenario["status"] == "ALLOCATED"
                inside_buffer = (
                    abs(forecast_bias) <= max_bias and spread_addition <= max_spread
                )
                guarded_allocate = baseline_allocate and inside_buffer
                episode_id = (
                    f"pmb23-{category}-{abs(forecast_bias)}-{spread_addition}"
                )
                baseline_row = _episode_row(
                    index, episode_id, category, settlement, scenario,
                    "BASELINE_GATE", baseline_allocate,
                    None if baseline_allocate else scenario["blocker"],
                )
                guarded_row = _episode_row(
                    index, episode_id, category, settlement, scenario,
                    "CERTIFIED_BUFFER" if inside_buffer else "OUTSIDE_CERTIFIED_BUFFER",
                    guarded_allocate,
                    None if guarded_allocate else (
                        "STRESS_BUFFER_EXCEEDED" if baseline_allocate and not inside_buffer
                        else scenario["blocker"]
                    ),
                )
                for row in (baseline_row, guarded_row):
                    row["forecast_bias"] = str(forecast_bias)
                    row["spread_addition"] = str(spread_addition)
                    row["inside_certified_buffer"] = inside_buffer
                baseline_rows.append(baseline_row)
                guarded_rows.append(guarded_row)
                index += 1
    baseline = _metrics("baseline", baseline_rows)
    guarded = _metrics("stress_aware_guard", guarded_rows)
    baseline_roc = Decimal(baseline["total_pnl"]) / Decimal(baseline["capital_usage"])
    guarded_roc = Decimal(guarded["total_pnl"]) / Decimal(guarded["capital_usage"])
    baseline["return_on_capital"] = str(baseline_roc)
    guarded["return_on_capital"] = str(guarded_roc)
    rejected_by_guard = sum(row["blocker"] == "STRESS_BUFFER_EXCEEDED" for row in guarded_rows)
    canonical = json.dumps(
        {"baseline": baseline, "guarded": guarded},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return {
        "phase": "PMB-23",
        "mode": "LOCAL_SYNTHETIC_STRESS_AWARE_ALLOCATION_GUARD_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "runtime_policy_changed": False,
        "certified_buffer": {
            "maximum_forecast_bias_magnitude": str(max_bias),
            "maximum_spread_addition": str(max_spread),
            "source_digest": boundary["summary"]["deterministic_digest"],
        },
        "baseline": baseline,
        "guarded_policy": guarded,
        "comparison": {
            "trade_count_delta": guarded["trade_count"] - baseline["trade_count"],
            "capital_usage_delta": str(
                Decimal(guarded["capital_usage"]) - Decimal(baseline["capital_usage"])
            ),
            "pnl_delta": str(Decimal(guarded["total_pnl"]) - Decimal(baseline["total_pnl"])),
            "return_on_capital_delta": str(guarded_roc - baseline_roc),
            "max_drawdown_delta": str(
                Decimal(guarded["max_drawdown"]) - Decimal(baseline["max_drawdown"])
            ),
            "otherwise_allocating_rows_rejected_by_guard": rejected_by_guard,
            "capital_usage_reduced_or_equal": (
                Decimal(guarded["capital_usage"]) <= Decimal(baseline["capital_usage"])
            ),
            "pnl_improved_or_equal": (
                Decimal(guarded["total_pnl"]) >= Decimal(baseline["total_pnl"])
            ),
            "return_on_capital_improved_or_equal": guarded_roc >= baseline_roc,
            "drawdown_improved_or_equal": (
                Decimal(guarded["max_drawdown"]) <= Decimal(baseline["max_drawdown"])
            ),
        },
        "summary": {
            "synthetic_rows": len(baseline_rows),
            "categories": sorted(CATEGORY_TICKER),
            "inside_buffer_rows": sum(row["inside_certified_buffer"] for row in guarded_rows),
            "outside_buffer_rows": sum(not row["inside_certified_buffer"] for row in guarded_rows),
            "all_attribution_complete": all(
                row["attribution_complete"] for row in baseline_rows + guarded_rows
            ),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_stress_aware_allocation_guard_preview(output_dir: Path) -> Path:
    report = build_stress_aware_allocation_guard_preview()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb23_stress_aware_allocation_guard_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _settlement(category: str, index: int) -> str:
    offsets = {"crypto": 0, "sports": 1, "weather": 2}
    return "yes" if (index + offsets[category]) % 3 != 0 else "no"
