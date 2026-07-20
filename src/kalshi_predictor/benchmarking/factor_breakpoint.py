from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.joint_surface import build_joint_robust_decision_surface
from kalshi_predictor.benchmarking.tail_stress import _run_level


FACTOR_GRIDS: dict[str, tuple[str | int, ...]] = {
    "forecast_bias": ("0", "-0.01", "-0.02", "-0.04", "-0.06", "-0.08", "-0.10"),
    "spread_addition": ("0", "0.01", "0.02", "0.04", "0.08", "0.12", "0.16"),
    "depth_multiplier": ("1", "0.90", "0.80", "0.60", "0.40", "0.20", "0.10"),
    "adverse_settlement_count": (0, 1, 2, 3, 4, 5),
}


def build_factor_isolated_breakpoint_attribution() -> dict[str, Any]:
    training = build_joint_robust_decision_surface()
    zones = {
        (row["ticker"], row["spread"], row["top_five_depth"]): row["classification"]
        for row in training["robust_zones"]
    }
    factors = []
    for factor, values in FACTOR_GRIDS.items():
        rows = []
        for step, value in enumerate(values):
            level: dict[str, str | int] = {
                "severity": step,
                "forecast_bias": "0",
                "spread_addition": "0",
                "depth_multiplier": "1",
                "adverse_settlement_count": 0,
            }
            level[factor] = value
            row = _run_level(level, zones)
            row["isolated_factor"] = factor
            row["isolated_value"] = value
            rows.append(row)
        drawdown_break = _first_break(rows, "drawdown_advantage_survived")
        efficiency_break = _first_break(rows, "capital_efficiency_advantage_survived")
        any_break = next(
            (row for row in rows if not row["comparison"]["both_advantages_survived"]),
            None,
        )
        factors.append({
            "factor": factor,
            "grid": list(values),
            "rows": rows,
            "first_drawdown_break": _summary(drawdown_break),
            "first_capital_efficiency_break": _summary(efficiency_break),
            "first_any_break": _summary(any_break),
            "bounded_grid_preserved_strict_advantage": any_break is None,
        })
    factors.sort(key=lambda row: row["factor"])
    causal = [row["factor"] for row in factors if row["first_any_break"] is not None]
    canonical = json.dumps(factors, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-20",
        "mode": "LOCAL_SYNTHETIC_FACTOR_ISOLATED_BREAKPOINT_ATTRIBUTION",
        "database_access": False,
        "database_writes": 0,
        "execution_enabled": False,
        "external_replay_data_used": False,
        "thresholds_changed": False,
        "policy_retrained": False,
        "training_surface_digest": training["summary"]["deterministic_digest"],
        "factors": factors,
        "attribution": {
            "factors_with_independent_breaks": causal,
            "factors_preserving_advantage_across_grid": [
                row["factor"] for row in factors if row["first_any_break"] is None
            ],
            "severity_one_tie_is_single_factor_explained": len(causal) == 1,
        },
        "summary": {
            "factor_count": len(factors),
            "evaluated_levels": sum(len(row["rows"]) for row in factors),
            "all_attribution_complete": all(
                level["summary"]["all_attribution_complete"]
                for factor in factors for level in factor["rows"]
            ),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_factor_isolated_breakpoint_attribution(output_dir: Path) -> Path:
    report = build_factor_isolated_breakpoint_attribution()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb20_factor_isolated_breakpoint_attribution.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _first_break(rows: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    return next((row for row in rows if not row["comparison"][field]), None)


def _summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "step": row["severity"],
        "value": row["isolated_value"],
        "comparison": row["comparison"],
    }
