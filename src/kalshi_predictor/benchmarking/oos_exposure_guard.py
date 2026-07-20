from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.counterfactual import BASELINE_FORECASTS
from kalshi_predictor.benchmarking.liquidity_boundary import _evaluate
from kalshi_predictor.benchmarking.oos_policy import CATEGORY_TICKER, _episode_row, _metrics


FROZEN_POSITION_SCALE = Decimal("0.95")
OOS_EXPOSURE_EPISODES = (
    ("pmb25-oos-crypto-a", "crypto", "-0.004", "0.002", "25", "yes"),
    ("pmb25-oos-crypto-b", "crypto", "-0.008", "0.006", "25", "no"),
    ("pmb25-oos-crypto-c", "crypto", "-0.002", "0.008", "5", "no"),
    ("pmb25-oos-crypto-d", "crypto", "-0.006", "0.004", "25", "yes"),
    ("pmb25-oos-weather-a", "weather", "-0.006", "0.002", "25", "no"),
    ("pmb25-oos-weather-b", "weather", "-0.002", "0.006", "5", "yes"),
    ("pmb25-oos-weather-c", "weather", "-0.008", "0.004", "25", "no"),
    ("pmb25-oos-weather-d", "weather", "-0.004", "0.008", "25", "yes"),
    ("pmb25-oos-sports-a", "sports", "-0.002", "0.004", "25", "yes"),
    ("pmb25-oos-sports-b", "sports", "-0.006", "0.008", "5", "no"),
    ("pmb25-oos-sports-c", "sports", "-0.004", "0.006", "25", "yes"),
    ("pmb25-oos-sports-d", "sports", "-0.008", "0.002", "25", "no"),
)


def build_oos_exposure_guard_validation() -> dict[str, Any]:
    full_rows = []
    guarded_rows = []
    for index, values in enumerate(OOS_EXPOSURE_EPISODES):
        episode_id, category, forecast_bias, spread_addition, depth, settlement = values
        ticker = CATEGORY_TICKER[category]
        scenario = _evaluate(
            ticker,
            Decimal("0.02") + Decimal(spread_addition),
            Decimal(depth),
            BASELINE_FORECASTS[ticker] + Decimal(forecast_bias),
        )
        allocated = scenario["status"] == "ALLOCATED"
        full = _episode_row(
            index, episode_id, category, settlement, scenario,
            "OOS_CERTIFIED_BUFFER", allocated,
            None if allocated else scenario["blocker"],
        )
        guarded = dict(full)
        guarded["capital_used"] = str(Decimal(full["capital_used"]) * FROZEN_POSITION_SCALE)
        guarded["settlement_pnl"] = str(
            Decimal(full["settlement_pnl"]) * FROZEN_POSITION_SCALE
        )
        guarded["position_scale"] = str(FROZEN_POSITION_SCALE)
        full["position_scale"] = "1"
        for row in (full, guarded):
            row["forecast_bias"] = forecast_bias
            row["spread_addition"] = spread_addition
            row["top_five_depth"] = depth
        full_rows.append(full)
        guarded_rows.append(guarded)
    full = _with_roc(_metrics("full_exposure", full_rows))
    guarded = _with_roc(_metrics("frozen_95_percent_exposure", guarded_rows))
    drawdown_improved = Decimal(guarded["max_drawdown"]) < Decimal(full["max_drawdown"])
    roc_preserved = abs(
        Decimal(guarded["return_on_capital"]) - Decimal(full["return_on_capital"])
    ) <= Decimal("1e-24")
    canonical = json.dumps(
        {"full": full, "guarded": guarded}, sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "phase": "PMB-25",
        "mode": "LOCAL_SYNTHETIC_OUT_OF_SAMPLE_FROZEN_EXPOSURE_GUARD_VALIDATION",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "policy_tuned_on_oos": False,
        "frozen_position_scale": str(FROZEN_POSITION_SCALE),
        "full_exposure": full,
        "guarded_policy": guarded,
        "comparison": {
            "trade_count_delta": guarded["trade_count"] - full["trade_count"],
            "capital_usage_delta": str(
                Decimal(guarded["capital_usage"]) - Decimal(full["capital_usage"])
            ),
            "pnl_delta": str(Decimal(guarded["total_pnl"]) - Decimal(full["total_pnl"])),
            "max_drawdown_delta": str(
                Decimal(guarded["max_drawdown"]) - Decimal(full["max_drawdown"])
            ),
            "return_on_capital_delta": str(
                Decimal(guarded["return_on_capital"]) - Decimal(full["return_on_capital"])
            ),
            "drawdown_improvement_survived": drawdown_improved,
            "return_on_capital_preserved": roc_preserved,
        },
        "summary": {
            "out_of_sample_episodes": len(OOS_EXPOSURE_EPISODES),
            "categories": sorted(CATEGORY_TICKER),
            "new_episode_ids": all(row[0].startswith("pmb25-oos-") for row in OOS_EXPOSURE_EPISODES),
            "identical_trade_selection": full["trade_count"] == guarded["trade_count"],
            "all_attribution_complete": all(
                row["attribution_complete"] for row in full_rows + guarded_rows
            ),
            "validation_passed": drawdown_improved and roc_preserved,
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_oos_exposure_guard_validation(output_dir: Path) -> Path:
    report = build_oos_exposure_guard_validation()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb25_oos_exposure_guard_validation.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _with_roc(metrics: dict[str, Any]) -> dict[str, Any]:
    capital = Decimal(metrics["capital_usage"])
    metrics["return_on_capital"] = str(
        Decimal(metrics["total_pnl"]) / capital if capital > 0 else Decimal("0")
    )
    return metrics
