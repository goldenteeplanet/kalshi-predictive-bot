from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.joint_surface import build_joint_robust_decision_surface

SETTLEMENTS = {
    "SYN-BTC": "yes",
    "SYN-NYC-WEATHER": "no",
    "SYN-SPORTS": "yes",
}


def build_robust_zone_policy_comparison() -> dict[str, Any]:
    surface = build_joint_robust_decision_surface()
    zone_lookup = {
        (row["ticker"], row["spread"], row["top_five_depth"]): row["classification"]
        for row in surface["robust_zones"]
    }
    baseline_rows = []
    robust_rows = []
    for index, scenario in enumerate(surface["grid"]["rows"]):
        zone = zone_lookup[(
            scenario["ticker"], scenario["spread"], scenario["top_five_depth"]
        )]
        baseline_allocate = scenario["status"] == "ALLOCATED"
        robust_allocate = baseline_allocate and zone == "ROBUST_ALLOCATE"
        baseline_rows.append(_policy_row(index, scenario, zone, baseline_allocate, None))
        robust_rows.append(_policy_row(
            index, scenario, zone, robust_allocate,
            None if robust_allocate else (
                "ROBUST_ZONE_REQUIRED" if baseline_allocate else scenario["blocker"]
            ),
        ))
    baseline = _policy_metrics("baseline", baseline_rows)
    robust = _policy_metrics("robust_zone_only", robust_rows)
    canonical = json.dumps(
        {"baseline": baseline, "robust": robust},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return {
        "phase": "PMB-16",
        "mode": "LOCAL_SYNTHETIC_ROBUST_ZONE_POLICY_COMPARISON",
        "database_access": False,
        "database_writes": 0,
        "external_replay_data_used": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "baseline": baseline,
        "robust_policy": robust,
        "comparison": {
            "trade_count_delta": robust["trade_count"] - baseline["trade_count"],
            "rejected_opportunity_delta": (
                robust["rejected_opportunities"] - baseline["rejected_opportunities"]
            ),
            "capital_usage_delta": str(
                Decimal(robust["capital_usage"]) - Decimal(baseline["capital_usage"])
            ),
            "pnl_delta": str(Decimal(robust["total_pnl"]) - Decimal(baseline["total_pnl"])),
            "max_drawdown_delta": str(
                Decimal(robust["max_drawdown"]) - Decimal(baseline["max_drawdown"])
            ),
            "robust_zone_filtered_trades": sum(
                row["blocker"] == "ROBUST_ZONE_REQUIRED" for row in robust_rows
            ),
        },
        "summary": {
            "scenario_count": len(baseline_rows),
            "all_attribution_complete": all(
                row["attribution_complete"] for row in baseline_rows + robust_rows
            ),
            "robust_policy_allocates_only_robust_zones": all(
                not row["allocated"] or row["zone"] == "ROBUST_ALLOCATE"
                for row in robust_rows
            ),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_robust_zone_policy_comparison(output_dir: Path) -> Path:
    report = build_robust_zone_policy_comparison()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb16_robust_zone_policy_comparison.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _policy_row(
    index: int, scenario: dict[str, Any], zone: str, allocated: bool,
    blocker: str | None,
) -> dict[str, Any]:
    filled = Decimal(scenario["filled_size"]) if allocated else Decimal("0")
    cost = Decimal(scenario["executed_value"]) if allocated else Decimal("0")
    pnl = (
        filled - cost if allocated and SETTLEMENTS[scenario["ticker"]] == "yes"
        else -cost if allocated else Decimal("0")
    )
    return {
        "scenario_index": index,
        "ticker": scenario["ticker"],
        "forecast_delta": scenario["forecast_delta"],
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
        "orderbook_ref": scenario["orderbook_ref"],
        "attribution_complete": scenario["attribution_complete"],
    }


def _policy_metrics(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    cumulative = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    curve = []
    for row in rows:
        cumulative += Decimal(row["settlement_pnl"])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        curve.append({"scenario_index": row["scenario_index"], "cumulative_pnl": str(cumulative)})
    return {
        "name": name,
        "trade_count": sum(row["allocated"] for row in rows),
        "rejected_opportunities": sum(not row["allocated"] for row in rows),
        "capital_usage": str(sum(
            (Decimal(row["capital_used"]) for row in rows), Decimal("0")
        )),
        "total_pnl": str(cumulative),
        "max_drawdown": str(max_drawdown),
        "rows": rows,
        "pnl_curve": curve,
    }
