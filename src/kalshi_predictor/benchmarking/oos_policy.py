from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.counterfactual import BASELINE_FORECASTS
from kalshi_predictor.benchmarking.joint_surface import build_joint_robust_decision_surface
from kalshi_predictor.benchmarking.liquidity_boundary import _evaluate

CATEGORY_TICKER = {
    "crypto": "SYN-BTC",
    "weather": "SYN-NYC-WEATHER",
    "sports": "SYN-SPORTS",
}
OOS_EPISODES = (
    ("oos-crypto-a", "crypto", "0.02", "25", "-0.02", "yes"),
    ("oos-crypto-b", "crypto", "0.08", "5", "0.01", "no"),
    ("oos-crypto-c", "crypto", "0.20", "25", "0.03", "yes"),
    ("oos-weather-a", "weather", "0.02", "25", "-0.02", "no"),
    ("oos-weather-b", "weather", "0.08", "5", "0.01", "yes"),
    ("oos-weather-c", "weather", "0.20", "25", "0.03", "no"),
    ("oos-sports-a", "sports", "0.02", "5", "-0.02", "yes"),
    ("oos-sports-b", "sports", "0.08", "25", "0.01", "no"),
    ("oos-sports-c", "sports", "0.20", "5", "0.03", "yes"),
)


def build_oos_robust_policy_validation() -> dict[str, Any]:
    training = build_joint_robust_decision_surface()
    zones = {
        (row["ticker"], row["spread"], row["top_five_depth"]): row["classification"]
        for row in training["robust_zones"]
    }
    baseline_rows = []
    robust_rows = []
    for index, values in enumerate(OOS_EPISODES):
        episode_id, category, spread, depth, delta, settlement = values
        training_ticker = CATEGORY_TICKER[category]
        forecast = BASELINE_FORECASTS[training_ticker] + Decimal(delta)
        scenario = _evaluate(
            training_ticker, Decimal(spread), Decimal(depth), forecast
        )
        zone = zones[(training_ticker, spread, depth)]
        baseline_allocate = scenario["status"] == "ALLOCATED"
        robust_allocate = baseline_allocate and zone == "ROBUST_ALLOCATE"
        baseline_rows.append(_episode_row(
            index, episode_id, category, settlement, scenario, zone,
            baseline_allocate, None,
        ))
        robust_rows.append(_episode_row(
            index, episode_id, category, settlement, scenario, zone,
            robust_allocate,
            None if robust_allocate else (
                "ROBUST_ZONE_REQUIRED" if baseline_allocate else scenario["blocker"]
            ),
        ))
    baseline = _metrics("baseline", baseline_rows)
    robust = _metrics("frozen_robust_zone", robust_rows)
    canonical = json.dumps(
        {"baseline": baseline, "robust": robust}, sort_keys=True, separators=(",", ":")
    ).encode()
    capital_benefit = Decimal(robust["capital_usage"]) <= Decimal(baseline["capital_usage"])
    drawdown_benefit = Decimal(robust["max_drawdown"]) <= Decimal(baseline["max_drawdown"])
    return {
        "phase": "PMB-17",
        "mode": "LOCAL_SYNTHETIC_OUT_OF_SAMPLE_FROZEN_POLICY_VALIDATION",
        "database_access": False,
        "database_writes": 0,
        "external_replay_data_used": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "training_surface_digest": training["summary"]["deterministic_digest"],
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
            "capital_benefit_survived": capital_benefit,
            "drawdown_benefit_survived": drawdown_benefit,
        },
        "summary": {
            "out_of_sample_episodes": len(OOS_EPISODES),
            "categories": sorted(CATEGORY_TICKER),
            "frozen_policy_used": True,
            "capital_and_drawdown_benefit_survived": capital_benefit and drawdown_benefit,
            "all_attribution_complete": all(
                row["attribution_complete"] for row in baseline_rows + robust_rows
            ),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_oos_robust_policy_validation(output_dir: Path) -> Path:
    report = build_oos_robust_policy_validation()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb17_oos_robust_policy_validation.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _episode_row(
    index: int, episode_id: str, category: str, settlement: str,
    scenario: dict[str, Any], zone: str, allocated: bool, blocker: str | None,
) -> dict[str, Any]:
    filled = Decimal(scenario["filled_size"]) if allocated else Decimal("0")
    cost = Decimal(scenario["executed_value"]) if allocated else Decimal("0")
    pnl = (
        filled - cost if allocated and settlement == "yes"
        else -cost if allocated else Decimal("0")
    )
    return {
        "episode_index": index,
        "episode_id": episode_id,
        "category": category,
        "ticker": f"{episode_id.upper()}-MARKET",
        "settlement": settlement,
        "forecast_probability": scenario["forecast_probability"],
        "spread": scenario["spread"],
        "top_five_depth": scenario["top_five_depth"],
        "zone": zone,
        "allocated": allocated,
        "blocker": blocker,
        "capital_used": str(cost),
        "settlement_pnl": str(pnl),
        "model_name": scenario["model_name"],
        "model_version": scenario["model_version"],
        "feature_ref": scenario["feature_ref"],
        "observation_ref": scenario["observation_ref"],
        "orderbook_ref": {
            **scenario["orderbook_ref"], "oos_episode_id": episode_id,
        },
        "attribution_complete": scenario["attribution_complete"],
    }


def _metrics(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    cumulative = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for row in rows:
        cumulative += Decimal(row["settlement_pnl"])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return {
        "name": name,
        "trade_count": sum(row["allocated"] for row in rows),
        "capital_usage": str(sum(
            (Decimal(row["capital_used"]) for row in rows), Decimal("0")
        )),
        "total_pnl": str(cumulative),
        "max_drawdown": str(max_drawdown),
        "rows": rows,
    }
