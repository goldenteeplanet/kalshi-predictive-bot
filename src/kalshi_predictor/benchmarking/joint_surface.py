from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.counterfactual import BASELINE_FORECASTS
from kalshi_predictor.benchmarking.liquidity_boundary import (
    SPREADS,
    TOP_FIVE_DEPTHS,
    YES_BIDS,
    _evaluate,
)
from kalshi_predictor.benchmarking.sensitivity import PERTURBATIONS


def build_joint_robust_decision_surface(
    *,
    forecast_perturbations: tuple[Decimal, ...] = PERTURBATIONS,
    spreads: tuple[Decimal, ...] = SPREADS,
    top_five_depths: tuple[Decimal, ...] = TOP_FIVE_DEPTHS,
) -> dict[str, Any]:
    if not forecast_perturbations or not spreads or not top_five_depths:
        raise ValueError("joint decision grids must be non-empty")
    if any(abs(value) > Decimal("0.10") for value in forecast_perturbations):
        raise ValueError("forecast perturbations must be bounded to +/-0.10")
    rows = []
    for ticker in YES_BIDS:
        for delta in forecast_perturbations:
            forecast = BASELINE_FORECASTS[ticker] + delta
            for spread in spreads:
                for depth in top_five_depths:
                    row = _evaluate(ticker, spread, depth, forecast)
                    row["forecast_delta"] = str(delta)
                    rows.append(row)
    rows.sort(key=lambda row: (
        row["ticker"], Decimal(row["spread"]), Decimal(row["top_five_depth"]),
        Decimal(row["forecast_delta"]),
    ))
    zones = _zones(rows, forecast_perturbations, spreads, top_five_depths)
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-15",
        "mode": "LOCAL_SYNTHETIC_JOINT_FORECAST_LIQUIDITY_DECISION_SURFACE",
        "database_access": False,
        "database_writes": 0,
        "external_replay_data_used": False,
        "execution_enabled": False,
        "grid": {
            "forecast_perturbations": [str(value) for value in forecast_perturbations],
            "spreads": [str(value) for value in spreads],
            "top_five_depths": [str(value) for value in top_five_depths],
            "rows": rows,
        },
        "robust_zones": zones,
        "summary": {
            "rows": len(rows),
            "zones": len(zones),
            "robust_allocate_zones": sum(
                row["classification"] == "ROBUST_ALLOCATE" for row in zones
            ),
            "robust_reject_zones": sum(
                row["classification"] == "ROBUST_REJECT" for row in zones
            ),
            "fragile_zones": sum(row["classification"] == "FRAGILE" for row in zones),
            "all_attribution_complete": all(row["attribution_complete"] for row in rows),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_joint_robust_decision_surface(output_dir: Path) -> Path:
    report = build_joint_robust_decision_surface()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb15_joint_forecast_liquidity_surface.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _zones(
    rows: list[dict[str, Any]],
    perturbations: tuple[Decimal, ...],
    spreads: tuple[Decimal, ...],
    depths: tuple[Decimal, ...],
) -> list[dict[str, Any]]:
    zones = []
    for ticker in YES_BIDS:
        for spread in spreads:
            for depth in depths:
                selected = [
                    row for row in rows
                    if row["ticker"] == ticker
                    and Decimal(row["spread"]) == spread
                    and Decimal(row["top_five_depth"]) == depth
                ]
                allocated = sum(row["status"] == "ALLOCATED" for row in selected)
                classification = (
                    "ROBUST_ALLOCATE" if allocated == len(perturbations)
                    else "ROBUST_REJECT" if allocated == 0 else "FRAGILE"
                )
                zones.append({
                    "ticker": ticker,
                    "spread": str(spread),
                    "top_five_depth": str(depth),
                    "classification": classification,
                    "allocated_forecast_variants": allocated,
                    "total_forecast_variants": len(perturbations),
                    "outcomes_by_forecast_delta": {
                        row["forecast_delta"]: {
                            "status": row["status"], "blocker": row["blocker"],
                            "fill_state": row["fill_state"],
                        }
                        for row in sorted(
                            selected, key=lambda value: Decimal(value["forecast_delta"])
                        )
                    },
                })
    return zones
