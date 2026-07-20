from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.joint_surface import build_joint_robust_decision_surface
from kalshi_predictor.benchmarking.tail_stress import _run_level


FORECAST_BIAS_GRID = tuple(Decimal(value) for value in (
    "0", "-0.002", "-0.004", "-0.006", "-0.008", "-0.010"
))
SPREAD_ADDITION_GRID = tuple(Decimal(value) for value in (
    "0", "0.002", "0.004", "0.006", "0.008", "0.010"
))


def build_interaction_boundary_refinement() -> dict[str, Any]:
    training = build_joint_robust_decision_surface()
    zones = {
        (row["ticker"], row["spread"], row["top_five_depth"]): row["classification"]
        for row in training["robust_zones"]
    }
    isolated_forecast = {
        value: _run_level(_level(forecast_bias=value), zones)
        for value in FORECAST_BIAS_GRID
    }
    isolated_spread = {
        value: _run_level(_level(spread_addition=value), zones)
        for value in SPREAD_ADDITION_GRID
    }
    rows = []
    for forecast_bias in FORECAST_BIAS_GRID:
        for spread_addition in SPREAD_ADDITION_GRID:
            joint = _run_level(_level(
                forecast_bias=forecast_bias, spread_addition=spread_addition
            ), zones)
            forecast_survived = isolated_forecast[forecast_bias]["comparison"][
                "both_advantages_survived"
            ]
            spread_survived = isolated_spread[spread_addition]["comparison"][
                "both_advantages_survived"
            ]
            joint_survived = joint["comparison"]["both_advantages_survived"]
            rows.append({
                "forecast_bias": str(forecast_bias),
                "spread_addition": str(spread_addition),
                "combined_magnitude_l1": str(abs(forecast_bias) + spread_addition),
                "isolated_forecast_survived": forecast_survived,
                "isolated_spread_survived": spread_survived,
                "joint_survived": joint_survived,
                "joint_only_break": forecast_survived and spread_survived and not joint_survived,
                "drawdown_advantage_margin": str(-Decimal(
                    joint["comparison"]["drawdown_delta"]
                )),
                "capital_efficiency_advantage_margin": joint["comparison"][
                    "capital_efficiency_delta"
                ],
                "all_attribution_complete": joint["summary"]["all_attribution_complete"],
            })
    breaks = sorted(
        (row for row in rows if row["joint_only_break"]),
        key=lambda row: (
            Decimal(row["combined_magnitude_l1"]),
            abs(Decimal(row["forecast_bias"])),
            Decimal(row["spread_addition"]),
        ),
    )
    boundary = breaks[0] if breaks else None
    buffer = _safety_buffer(rows, boundary)
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-22",
        "mode": "LOCAL_SYNTHETIC_FORECAST_SPREAD_INTERACTION_BOUNDARY_REFINEMENT",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "policy_retrained": False,
        "training_surface_digest": training["summary"]["deterministic_digest"],
        "grid": {
            "forecast_bias": [str(value) for value in FORECAST_BIAS_GRID],
            "spread_addition": [str(value) for value in SPREAD_ADDITION_GRID],
            "rows": rows,
        },
        "minimum_joint_only_break": boundary,
        "deterministic_safety_buffer": buffer,
        "summary": {
            "cells": len(rows),
            "joint_only_breaks": len(breaks),
            "boundary_found": boundary is not None,
            "all_attribution_complete": all(row["all_attribution_complete"] for row in rows),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def golden_interaction_boundary_summary(report: dict[str, Any]) -> dict[str, Any]:
    boundary = report["minimum_joint_only_break"]
    return {
        "phase": report["phase"],
        "grid_cells": report["summary"]["cells"],
        "joint_only_breaks": report["summary"]["joint_only_breaks"],
        "minimum_joint_only_break": None if boundary is None else {
            "forecast_bias": boundary["forecast_bias"],
            "spread_addition": boundary["spread_addition"],
            "combined_magnitude_l1": boundary["combined_magnitude_l1"],
        },
        "deterministic_safety_buffer": report["deterministic_safety_buffer"],
        "deterministic_digest": report["summary"]["deterministic_digest"],
    }


def write_interaction_boundary_refinement(
    output_dir: Path, *, golden_path: Path | None = None
) -> Path:
    report = build_interaction_boundary_refinement()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb22_interaction_boundary_refinement.json"
    _atomic_json(path, report)
    if golden_path is not None:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(golden_path, golden_interaction_boundary_summary(report))
    return path


def _level(**overrides: Decimal) -> dict[str, str | int]:
    level: dict[str, str | int] = {
        "severity": 0,
        "forecast_bias": "0",
        "spread_addition": "0",
        "depth_multiplier": "1",
        "adverse_settlement_count": 0,
    }
    level.update({key: str(value) for key, value in overrides.items()})
    return level


def _safety_buffer(
    rows: list[dict[str, Any]], boundary: dict[str, Any] | None
) -> dict[str, Any]:
    if boundary is None:
        return {
            "certified_within_grid": True,
            "maximum_forecast_bias_magnitude": str(max(map(abs, FORECAST_BIAS_GRID))),
            "maximum_spread_addition": str(max(SPREAD_ADDITION_GRID)),
            "boundary_exclusive": False,
        }
    boundary_forecast = abs(Decimal(boundary["forecast_bias"]))
    boundary_spread = Decimal(boundary["spread_addition"])
    lower_forecasts = [abs(value) for value in FORECAST_BIAS_GRID if abs(value) < boundary_forecast]
    lower_spreads = [value for value in SPREAD_ADDITION_GRID if value < boundary_spread]
    forecast_buffer = max(lower_forecasts, default=Decimal("0"))
    spread_buffer = max(lower_spreads, default=Decimal("0"))
    rectangle = [
        row for row in rows
        if abs(Decimal(row["forecast_bias"])) <= forecast_buffer
        and Decimal(row["spread_addition"]) <= spread_buffer
    ]
    return {
        "certified_within_grid": bool(rectangle) and all(row["joint_survived"] for row in rectangle),
        "maximum_forecast_bias_magnitude": str(forecast_buffer),
        "maximum_spread_addition": str(spread_buffer),
        "boundary_exclusive": True,
        "certified_cells": len(rectangle),
        "grid_step": "0.002",
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
