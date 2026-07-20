from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.counterfactual import BASELINE_FORECASTS
from kalshi_predictor.benchmarking.joint_surface import build_joint_robust_decision_surface
from kalshi_predictor.benchmarking.liquidity_boundary import _evaluate
from kalshi_predictor.benchmarking.oos_policy import (
    CATEGORY_TICKER,
    OOS_EPISODES,
    _episode_row,
    _metrics,
)


TAIL_STRESS_LADDER: tuple[dict[str, str | int], ...] = (
    {"severity": 0, "forecast_bias": "0", "spread_addition": "0", "depth_multiplier": "1", "adverse_settlement_count": 0},
    {"severity": 1, "forecast_bias": "-0.02", "spread_addition": "0.02", "depth_multiplier": "0.80", "adverse_settlement_count": 1},
    {"severity": 2, "forecast_bias": "-0.04", "spread_addition": "0.05", "depth_multiplier": "0.60", "adverse_settlement_count": 2},
    {"severity": 3, "forecast_bias": "-0.06", "spread_addition": "0.08", "depth_multiplier": "0.40", "adverse_settlement_count": 3},
    {"severity": 4, "forecast_bias": "-0.08", "spread_addition": "0.12", "depth_multiplier": "0.20", "adverse_settlement_count": 4},
    {"severity": 5, "forecast_bias": "-0.10", "spread_addition": "0.16", "depth_multiplier": "0.10", "adverse_settlement_count": 5},
)


def build_tail_stress_breakpoint_search() -> dict[str, Any]:
    training = build_joint_robust_decision_surface()
    zones = {
        (row["ticker"], row["spread"], row["top_five_depth"]): row["classification"]
        for row in training["robust_zones"]
    }
    levels = [_run_level(level, zones) for level in TAIL_STRESS_LADDER]
    first_drawdown_break = _first_break(levels, "drawdown_advantage_survived")
    first_efficiency_break = _first_break(levels, "capital_efficiency_advantage_survived")
    first_any_break = next(
        (row for row in levels if not row["comparison"]["both_advantages_survived"]),
        None,
    )
    canonical = json.dumps(levels, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-19",
        "mode": "LOCAL_SYNTHETIC_PROGRESSIVE_TAIL_STRESS_BREAKPOINT_SEARCH",
        "database_access": False,
        "database_writes": 0,
        "execution_enabled": False,
        "external_replay_data_used": False,
        "thresholds_changed": False,
        "policy_retrained": False,
        "training_surface_digest": training["summary"]["deterministic_digest"],
        "stress_levels": levels,
        "breakpoints": {
            "first_drawdown_break": _break_summary(first_drawdown_break),
            "first_capital_efficiency_break": _break_summary(first_efficiency_break),
            "first_any_break": _break_summary(first_any_break),
            "bounded_search_exhausted_without_break": first_any_break is None,
        },
        "summary": {
            "levels": len(levels),
            "episodes_per_level": len(OOS_EPISODES),
            "categories": sorted(CATEGORY_TICKER),
            "all_attribution_complete": all(
                row["summary"]["all_attribution_complete"] for row in levels
            ),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_tail_stress_breakpoint_search(output_dir: Path) -> Path:
    report = build_tail_stress_breakpoint_search()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb19_progressive_tail_stress_breakpoints.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _run_level(
    level: dict[str, str | int], zones: dict[tuple[str, str, str], str]
) -> dict[str, Any]:
    baseline_rows: list[dict[str, Any]] = []
    robust_rows: list[dict[str, Any]] = []
    adverse_remaining = int(level["adverse_settlement_count"])
    for index, values in enumerate(OOS_EPISODES):
        episode_id, category, spread_text, depth_text, delta_text, settlement = values
        ticker = CATEGORY_TICKER[category]
        spread = min(Decimal("0.99"), Decimal(spread_text) + Decimal(str(level["spread_addition"])))
        depth = Decimal(depth_text) * Decimal(str(level["depth_multiplier"]))
        forecast = max(Decimal("0"), min(
            Decimal("1"),
            BASELINE_FORECASTS[ticker] + Decimal(delta_text) + Decimal(str(level["forecast_bias"])),
        ))
        if settlement == "yes" and adverse_remaining > 0:
            settlement = "no"
            adverse_remaining -= 1
        scenario = _evaluate(ticker, spread, depth, forecast)
        zone = zones[(ticker, spread_text, depth_text)]
        baseline_allocate = scenario["status"] == "ALLOCATED"
        robust_allocate = baseline_allocate and zone == "ROBUST_ALLOCATE"
        stressed_id = f"{episode_id}:tail-{level['severity']}"
        baseline_rows.append(_episode_row(
            index, stressed_id, category, settlement, scenario, zone,
            baseline_allocate, None if baseline_allocate else scenario["blocker"],
        ))
        robust_rows.append(_episode_row(
            index, stressed_id, category, settlement, scenario, zone,
            robust_allocate,
            None if robust_allocate else (
                "ROBUST_ZONE_REQUIRED" if baseline_allocate else scenario["blocker"]
            ),
        ))
    baseline = _with_efficiency(_metrics("baseline", baseline_rows))
    robust = _with_efficiency(_metrics("frozen_robust_zone", robust_rows))
    # PMB-19 searches for where the advantage stops improving. A tie therefore
    # counts as a break, even though the robust policy is not strictly worse.
    drawdown_advantage = Decimal(robust["max_drawdown"]) < Decimal(baseline["max_drawdown"])
    efficiency_advantage = Decimal(robust["risk_adjusted_capital_efficiency"]) > Decimal(
        baseline["risk_adjusted_capital_efficiency"]
    )
    return {
        **level,
        "baseline": baseline,
        "robust_policy": robust,
        "comparison": {
            "drawdown_delta": str(Decimal(robust["max_drawdown"]) - Decimal(baseline["max_drawdown"])),
            "capital_efficiency_delta": str(
                Decimal(robust["risk_adjusted_capital_efficiency"])
                - Decimal(baseline["risk_adjusted_capital_efficiency"])
            ),
            "drawdown_advantage_survived": drawdown_advantage,
            "capital_efficiency_advantage_survived": efficiency_advantage,
            "both_advantages_survived": drawdown_advantage and efficiency_advantage,
        },
        "summary": {
            "all_attribution_complete": all(
                row["attribution_complete"] for row in baseline_rows + robust_rows
            )
        },
    }


def _with_efficiency(metrics: dict[str, Any]) -> dict[str, Any]:
    capital = Decimal(metrics["capital_usage"])
    pnl = Decimal(metrics["total_pnl"])
    drawdown = Decimal(metrics["max_drawdown"])
    denominator = capital + drawdown
    metrics["risk_adjusted_capital_efficiency"] = str(
        pnl / denominator if denominator > 0 else Decimal("0")
    )
    return metrics


def _first_break(levels: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    return next((row for row in levels if not row["comparison"][field]), None)


def _break_summary(level: dict[str, Any] | None) -> dict[str, Any] | None:
    if level is None:
        return None
    return {
        "severity": level["severity"],
        "forecast_bias": level["forecast_bias"],
        "spread_addition": level["spread_addition"],
        "depth_multiplier": level["depth_multiplier"],
        "adverse_settlement_count": level["adverse_settlement_count"],
        "comparison": level["comparison"],
    }
