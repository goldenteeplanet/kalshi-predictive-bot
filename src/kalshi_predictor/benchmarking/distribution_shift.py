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


SHIFT_REGIMES: tuple[dict[str, str], ...] = (
    {
        "name": "control",
        "forecast_shift": "0",
        "depth_multiplier": "1",
        "spread_addition": "0",
        "settlement_mode": "original",
    },
    {
        "name": "forecast_error_shift",
        "forecast_shift": "-0.08",
        "depth_multiplier": "1",
        "spread_addition": "0",
        "settlement_mode": "original",
    },
    {
        "name": "thinner_books",
        "forecast_shift": "0",
        "depth_multiplier": "0.20",
        "spread_addition": "0",
        "settlement_mode": "original",
    },
    {
        "name": "wider_spreads",
        "forecast_shift": "0",
        "depth_multiplier": "1",
        "spread_addition": "0.12",
        "settlement_mode": "original",
    },
    {
        "name": "adverse_settlement_mix",
        "forecast_shift": "0",
        "depth_multiplier": "1",
        "spread_addition": "0",
        "settlement_mode": "all_no",
    },
    {
        "name": "combined_adverse_shift",
        "forecast_shift": "-0.08",
        "depth_multiplier": "0.20",
        "spread_addition": "0.12",
        "settlement_mode": "all_no",
    },
)


def build_distribution_shift_stress_validation() -> dict[str, Any]:
    training = build_joint_robust_decision_surface()
    zones = {
        (row["ticker"], row["spread"], row["top_five_depth"]): row["classification"]
        for row in training["robust_zones"]
    }
    regimes = [_run_regime(regime, zones) for regime in SHIFT_REGIMES]
    control = regimes[0]
    for regime in regimes:
        regime["relative_to_control"] = {
            "robust_pnl_delta": str(
                Decimal(regime["robust_policy"]["total_pnl"])
                - Decimal(control["robust_policy"]["total_pnl"])
            ),
            "robust_drawdown_delta": str(
                Decimal(regime["robust_policy"]["max_drawdown"])
                - Decimal(control["robust_policy"]["max_drawdown"])
            ),
            "robust_capital_usage_delta": str(
                Decimal(regime["robust_policy"]["capital_usage"])
                - Decimal(control["robust_policy"]["capital_usage"])
            ),
        }
    broken = [
        row["name"] for row in regimes
        if not row["comparison"]["capital_and_drawdown_advantage_survived"]
    ]
    canonical = json.dumps(regimes, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-18",
        "mode": "LOCAL_SYNTHETIC_DETERMINISTIC_DISTRIBUTION_SHIFT_STRESS_VALIDATION",
        "database_access": False,
        "database_writes": 0,
        "external_replay_data_used": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "policy_retrained": False,
        "training_surface_digest": training["summary"]["deterministic_digest"],
        "shift_regimes": regimes,
        "break_analysis": {
            "advantage_break_regimes": broken,
            "first_advantage_break_regime": broken[0] if broken else None,
            "all_regimes_preserved_advantage": not broken,
        },
        "summary": {
            "regime_count": len(regimes),
            "episode_count_per_regime": len(OOS_EPISODES),
            "categories": sorted(CATEGORY_TICKER),
            "all_attribution_complete": all(
                row["summary"]["all_attribution_complete"] for row in regimes
            ),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_distribution_shift_stress_validation(output_dir: Path) -> Path:
    report = build_distribution_shift_stress_validation()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb18_distribution_shift_stress_validation.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _run_regime(
    regime: dict[str, str], zones: dict[tuple[str, str, str], str]
) -> dict[str, Any]:
    baseline_rows: list[dict[str, Any]] = []
    robust_rows: list[dict[str, Any]] = []
    forecast_shift = Decimal(regime["forecast_shift"])
    depth_multiplier = Decimal(regime["depth_multiplier"])
    spread_addition = Decimal(regime["spread_addition"])
    for index, values in enumerate(OOS_EPISODES):
        episode_id, category, spread_text, depth_text, delta_text, settlement = values
        ticker = CATEGORY_TICKER[category]
        shifted_spread = min(Decimal("0.99"), Decimal(spread_text) + spread_addition)
        shifted_depth = Decimal(depth_text) * depth_multiplier
        forecast = max(
            Decimal("0"),
            min(Decimal("1"), BASELINE_FORECASTS[ticker] + Decimal(delta_text) + forecast_shift),
        )
        scenario = _evaluate(ticker, shifted_spread, shifted_depth, forecast)
        original_zone = zones[(ticker, spread_text, depth_text)]
        shifted_settlement = "no" if regime["settlement_mode"] == "all_no" else settlement
        baseline_allocate = scenario["status"] == "ALLOCATED"
        robust_allocate = baseline_allocate and original_zone == "ROBUST_ALLOCATE"
        shifted_id = f"{episode_id}:{regime['name']}"
        baseline_rows.append(_episode_row(
            index, shifted_id, category, shifted_settlement, scenario, original_zone,
            baseline_allocate, None if baseline_allocate else scenario["blocker"],
        ))
        robust_rows.append(_episode_row(
            index, shifted_id, category, shifted_settlement, scenario, original_zone,
            robust_allocate,
            None if robust_allocate else (
                "ROBUST_ZONE_REQUIRED" if baseline_allocate else scenario["blocker"]
            ),
        ))
    baseline = _metrics("baseline", baseline_rows)
    robust = _metrics("frozen_robust_zone", robust_rows)
    capital_advantage = Decimal(robust["capital_usage"]) <= Decimal(baseline["capital_usage"])
    drawdown_advantage = Decimal(robust["max_drawdown"]) <= Decimal(baseline["max_drawdown"])
    return {
        **regime,
        "baseline": baseline,
        "robust_policy": robust,
        "comparison": {
            "trade_count_delta": robust["trade_count"] - baseline["trade_count"],
            "capital_usage_delta": str(
                Decimal(robust["capital_usage"]) - Decimal(baseline["capital_usage"])
            ),
            "pnl_delta": str(Decimal(robust["total_pnl"]) - Decimal(baseline["total_pnl"])),
            "max_drawdown_delta": str(
                Decimal(robust["max_drawdown"]) - Decimal(baseline["max_drawdown"])
            ),
            "capital_advantage_survived": capital_advantage,
            "drawdown_advantage_survived": drawdown_advantage,
            "capital_and_drawdown_advantage_survived": capital_advantage and drawdown_advantage,
        },
        "summary": {
            "all_attribution_complete": all(
                row["attribution_complete"] for row in baseline_rows + robust_rows
            ),
            "baseline_rejections": sum(not row["allocated"] for row in baseline_rows),
            "robust_rejections": sum(not row["allocated"] for row in robust_rows),
        },
    }
