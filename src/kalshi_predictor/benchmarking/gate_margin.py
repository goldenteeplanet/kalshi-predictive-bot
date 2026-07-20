from __future__ import annotations

import hashlib
import json
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.counterfactual import (
    BASELINE_FORECASTS,
    BASELINE_VERSIONS,
    _run_variant,
)
from kalshi_predictor.benchmarking.portfolio import PortfolioLimits

EDGE_THRESHOLD = Decimal("0.02")
PROBABILITY_QUANTUM = Decimal("0.0001")
EXPOSURE_BLOCKERS = {
    "CAPITAL_INSUFFICIENT", "GROSS_EXPOSURE_LIMIT", "CATEGORY_EXPOSURE_LIMIT",
    "CORRELATED_EXPOSURE_LIMIT", "TICKER_EXPOSURE_LIMIT",
}


def build_exact_gate_margin_report(
    *,
    forecasts: dict[str, Decimal] | None = None,
    versions: dict[str, str] | None = None,
    limits: PortfolioLimits | None = None,
) -> dict[str, Any]:
    selected_forecasts = forecasts or BASELINE_FORECASTS
    run = _run_variant(
        "gate-margin", selected_forecasts, versions or BASELINE_VERSIONS,
        limits or PortfolioLimits(),
    )
    rows = [_decision_margin(row) for row in run["decisions"]]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-13",
        "mode": "LOCAL_SYNTHETIC_EXACT_GATE_MARGIN_CERTIFICATION",
        "database_access": False,
        "database_writes": 0,
        "external_replay_data_used": False,
        "execution_enabled": False,
        "unchanged_thresholds": {
            "minimum_edge": str(EDGE_THRESHOLD),
            "probability_quantum": str(PROBABILITY_QUANTUM),
        },
        "decisions": rows,
        "summary": {
            "decisions": len(rows),
            "edge_boundaries_certified": sum(row["boundary_certified"] for row in rows),
            "forecast_flippable_decisions": sum(row["forecast_flippable"] for row in rows),
            "edge_blockers": sum(row["active_blocker_type"] == "EDGE" for row in rows),
            "liquidity_blockers": sum(row["active_blocker_type"] == "LIQUIDITY" for row in rows),
            "exposure_blockers": sum(row["active_blocker_type"] == "EXPOSURE" for row in rows),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_exact_gate_margin_report(output_dir: Path) -> Path:
    report = build_exact_gate_margin_report()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb13_exact_gate_margin_certification.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _decision_margin(decision: dict[str, Any]) -> dict[str, Any]:
    forecast = Decimal(decision["forecast_probability"])
    ask = Decimal(decision["best_yes_ask"]) if decision["best_yes_ask"] else None
    blocker_type = _blocker_type(decision["blocker"])
    boundary = ask + EDGE_THRESHOLD if ask is not None else None
    margin = forecast - boundary if boundary is not None else None
    current_edge_pass = bool(margin is not None and margin > 0)
    forecast_flippable = blocker_type in ("EDGE", "NONE") and boundary is not None
    required_change = None
    boundary_certified = False
    if forecast_flippable and margin is not None:
        if current_edge_pass:
            required_change = -_ceil_quantum(margin)
            flipped_forecast = forecast + required_change
            boundary_certified = flipped_forecast <= boundary
        else:
            required_change = _ceil_quantum(-margin) + PROBABILITY_QUANTUM
            flipped_forecast = forecast + required_change
            boundary_certified = flipped_forecast > boundary
    else:
        flipped_forecast = None
    return {
        "timestamp": decision["timestamp"],
        "ticker": decision["ticker"],
        "category": decision["category"],
        "status": decision["status"],
        "active_blocker": decision["blocker"],
        "active_blocker_type": blocker_type,
        "forecast_probability": str(forecast),
        "model_name": decision["model_name"],
        "model_version": decision["model_version"],
        "best_yes_ask": str(ask) if ask is not None else None,
        "edge_threshold": str(EDGE_THRESHOLD),
        "decision_boundary_probability": str(boundary) if boundary is not None else None,
        "signed_gate_margin": str(margin) if margin is not None else None,
        "current_edge_pass": current_edge_pass,
        "forecast_flippable": forecast_flippable,
        "minimum_forecast_change_to_flip": (
            str(required_change) if required_change is not None else None
        ),
        "flipped_forecast_probability": (
            str(flipped_forecast) if flipped_forecast is not None else None
        ),
        "boundary_certified": boundary_certified,
        "feature_ref": decision["feature_ref"],
        "observation_ref": decision["observation_ref"],
        "orderbook_ref": decision["orderbook_ref"],
    }


def _ceil_quantum(value: Decimal) -> Decimal:
    units = (value / PROBABILITY_QUANTUM).to_integral_value(rounding=ROUND_CEILING)
    return (units * PROBABILITY_QUANTUM).quantize(PROBABILITY_QUANTUM)


def _blocker_type(blocker: str | None) -> str:
    if blocker is None:
        return "NONE"
    if blocker == "EDGE_NOT_POSITIVE":
        return "EDGE"
    if blocker == "INSUFFICIENT_LIQUIDITY":
        return "LIQUIDITY"
    if blocker in EXPOSURE_BLOCKERS:
        return "EXPOSURE"
    return "OTHER"
