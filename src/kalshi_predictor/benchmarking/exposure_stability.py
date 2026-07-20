from __future__ import annotations

import hashlib
import json
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.counterfactual import BASELINE_FORECASTS
from kalshi_predictor.benchmarking.liquidity_boundary import _evaluate
from kalshi_predictor.benchmarking.oos_exposure_guard import (
    FROZEN_POSITION_SCALE,
    OOS_EXPOSURE_EPISODES,
)
from kalshi_predictor.benchmarking.oos_policy import CATEGORY_TICKER, _episode_row, _metrics


STABILITY_SEEDS = (11, 29, 47, 71, 97)


def build_multi_seed_exposure_stability_census() -> dict[str, Any]:
    seeds = [_run_seed(seed) for seed in STABILITY_SEEDS]
    canonical = json.dumps(seeds, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-26",
        "mode": "LOCAL_SYNTHETIC_MULTI_SEED_OOS_EXPOSURE_STABILITY_CENSUS",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "policy_tuned_per_seed": False,
        "frozen_position_scale": str(FROZEN_POSITION_SCALE),
        "seeds": seeds,
        "summary": {
            "seed_count": len(seeds),
            "all_seeds_passed": all(row["validation_passed"] for row in seeds),
            "drawdown_reduction_consistent": all(
                row["comparison"]["drawdown_improvement_survived"] for row in seeds
            ),
            "return_on_capital_preserved_consistently": all(
                row["comparison"]["return_on_capital_preserved"] for row in seeds
            ),
            "identical_trade_selection_all_seeds": all(
                row["comparison"]["identical_trade_selection"] for row in seeds
            ),
            "all_attribution_complete": all(
                row["all_attribution_complete"] for row in seeds
            ),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_multi_seed_exposure_stability_census(output_dir: Path) -> Path:
    report = build_multi_seed_exposure_stability_census()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb26_multi_seed_oos_exposure_stability_census.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _run_seed(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    episodes = list(OOS_EXPOSURE_EPISODES)
    rng.shuffle(episodes)
    full_rows = []
    guarded_rows = []
    settlement_sequence = []
    for index, values in enumerate(episodes):
        episode_id, category, forecast_bias, spread_addition, depth, _ = values
        settlement = "yes" if rng.random() < 0.55 else "no"
        settlement_sequence.append(settlement)
        ticker = CATEGORY_TICKER[category]
        scenario = _evaluate(
            ticker,
            Decimal("0.02") + Decimal(spread_addition),
            Decimal(depth),
            BASELINE_FORECASTS[ticker] + Decimal(forecast_bias),
        )
        allocated = scenario["status"] == "ALLOCATED"
        full = _episode_row(
            index, f"{episode_id}:seed-{seed}", category, settlement, scenario,
            "OOS_MULTI_SEED", allocated,
            None if allocated else scenario["blocker"],
        )
        guarded = dict(full)
        guarded["capital_used"] = str(Decimal(full["capital_used"]) * FROZEN_POSITION_SCALE)
        guarded["settlement_pnl"] = str(
            Decimal(full["settlement_pnl"]) * FROZEN_POSITION_SCALE
        )
        guarded["position_scale"] = str(FROZEN_POSITION_SCALE)
        full["position_scale"] = "1"
        full_rows.append(full)
        guarded_rows.append(guarded)
    full = _with_roc(_metrics("full_exposure", full_rows))
    guarded = _with_roc(_metrics("frozen_95_percent_exposure", guarded_rows))
    full_drawdown = Decimal(full["max_drawdown"])
    guarded_drawdown = Decimal(guarded["max_drawdown"])
    roc_delta = Decimal(guarded["return_on_capital"]) - Decimal(full["return_on_capital"])
    drawdown_improved = (
        guarded_drawdown < full_drawdown if full_drawdown > 0 else guarded_drawdown == 0
    )
    roc_preserved = abs(roc_delta) <= Decimal("1e-24")
    identical = full["trade_count"] == guarded["trade_count"]
    attribution_complete = all(
        row["attribution_complete"] for row in full_rows + guarded_rows
    )
    return {
        "seed": seed,
        "episode_order": [values[0] for values in episodes],
        "settlement_sequence": settlement_sequence,
        "full_exposure": full,
        "guarded_policy": guarded,
        "comparison": {
            "identical_trade_selection": identical,
            "capital_usage_delta": str(
                Decimal(guarded["capital_usage"]) - Decimal(full["capital_usage"])
            ),
            "pnl_delta": str(Decimal(guarded["total_pnl"]) - Decimal(full["total_pnl"])),
            "max_drawdown_delta": str(guarded_drawdown - full_drawdown),
            "return_on_capital_delta": str(roc_delta),
            "drawdown_improvement_survived": drawdown_improved,
            "return_on_capital_preserved": roc_preserved,
        },
        "all_attribution_complete": attribution_complete,
        "validation_passed": (
            identical and drawdown_improved and roc_preserved and attribution_complete
        ),
    }


def _with_roc(metrics: dict[str, Any]) -> dict[str, Any]:
    capital = Decimal(metrics["capital_usage"])
    metrics["return_on_capital"] = str(
        Decimal(metrics["total_pnl"]) / capital if capital > 0 else Decimal("0")
    )
    return metrics
