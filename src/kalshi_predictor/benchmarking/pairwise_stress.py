from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.joint_surface import build_joint_robust_decision_surface
from kalshi_predictor.benchmarking.tail_stress import _run_level


STRESS_FACTORS: dict[str, tuple[str | int, ...]] = {
    "forecast_bias": ("-0.01", "-0.02", "-0.04"),
    "spread_addition": ("0.01", "0.02", "0.04"),
    "adverse_settlement_count": (1, 2, 3),
    "depth_multiplier": ("0.80", "0.40", "0.10"),
}
FACTOR_PAIRS = (
    ("forecast_bias", "spread_addition"),
    ("forecast_bias", "adverse_settlement_count"),
    ("spread_addition", "adverse_settlement_count"),
    ("depth_multiplier", "forecast_bias"),
    ("depth_multiplier", "spread_addition"),
    ("depth_multiplier", "adverse_settlement_count"),
)


def build_pairwise_stress_interaction_matrix() -> dict[str, Any]:
    training = build_joint_robust_decision_surface()
    zones = {
        (row["ticker"], row["spread"], row["top_five_depth"]): row["classification"]
        for row in training["robust_zones"]
    }
    control = _run_level(_level(), zones)
    isolated: dict[tuple[str, str], dict[str, Any]] = {}
    pairs = []
    for factor_a, factor_b in FACTOR_PAIRS:
        cells = []
        for value_a in STRESS_FACTORS[factor_a]:
            for value_b in STRESS_FACTORS[factor_b]:
                isolated_a = isolated.setdefault(
                    (factor_a, str(value_a)),
                    _run_level(_level(**{factor_a: value_a}), zones),
                )
                isolated_b = isolated.setdefault(
                    (factor_b, str(value_b)),
                    _run_level(_level(**{factor_b: value_b}), zones),
                )
                joint = _run_level(_level(**{factor_a: value_a, factor_b: value_b}), zones)
                cells.append(_interaction_cell(
                    factor_a, value_a, factor_b, value_b,
                    control, isolated_a, isolated_b, joint,
                ))
        pairs.append({
            "factor_a": factor_a,
            "factor_b": factor_b,
            "cells": cells,
            "classification_counts": {
                classification: sum(row["classification"] == classification for row in cells)
                for classification in ("COMPOUNDING", "ADDITIVE", "OFFSETTING")
            },
            "joint_only_breaks": sum(row["joint_only_break"] for row in cells),
        })
    canonical = json.dumps(pairs, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-21",
        "mode": "LOCAL_SYNTHETIC_PAIRWISE_STRESS_INTERACTION_ATTRIBUTION_MATRIX",
        "database_access": False,
        "database_writes": 0,
        "execution_enabled": False,
        "external_replay_data_used": False,
        "thresholds_changed": False,
        "policy_retrained": False,
        "training_surface_digest": training["summary"]["deterministic_digest"],
        "interaction_definition": (
            "joint_margin - isolated_a_margin - isolated_b_margin + control_margin"
        ),
        "pairs": pairs,
        "summary": {
            "pair_count": len(pairs),
            "cell_count": sum(len(row["cells"]) for row in pairs),
            "compounding_cells": sum(
                row["classification_counts"]["COMPOUNDING"] for row in pairs
            ),
            "additive_cells": sum(row["classification_counts"]["ADDITIVE"] for row in pairs),
            "offsetting_cells": sum(row["classification_counts"]["OFFSETTING"] for row in pairs),
            "joint_only_breaks": sum(row["joint_only_breaks"] for row in pairs),
            "all_attribution_complete": all(
                cell["all_attribution_complete"] for pair in pairs for cell in pair["cells"]
            ),
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_pairwise_stress_interaction_matrix(output_dir: Path) -> Path:
    report = build_pairwise_stress_interaction_matrix()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb21_pairwise_stress_interaction_matrix.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _level(**overrides: str | int) -> dict[str, str | int]:
    level: dict[str, str | int] = {
        "severity": 0,
        "forecast_bias": "0",
        "spread_addition": "0",
        "depth_multiplier": "1",
        "adverse_settlement_count": 0,
    }
    level.update(overrides)
    return level


def _margin(row: dict[str, Any], field: str) -> Decimal:
    comparison = row["comparison"]
    if field == "drawdown":
        return -Decimal(comparison["drawdown_delta"])
    return Decimal(comparison["capital_efficiency_delta"])


def _interaction_cell(
    factor_a: str,
    value_a: str | int,
    factor_b: str,
    value_b: str | int,
    control: dict[str, Any],
    isolated_a: dict[str, Any],
    isolated_b: dict[str, Any],
    joint: dict[str, Any],
) -> dict[str, Any]:
    effects = {}
    for metric in ("drawdown", "capital_efficiency"):
        effect = (
            _margin(joint, metric) - _margin(isolated_a, metric)
            - _margin(isolated_b, metric) + _margin(control, metric)
        )
        effects[metric] = str(effect)
    numeric = [Decimal(value) for value in effects.values()]
    classification = (
        "COMPOUNDING" if any(value < 0 for value in numeric)
        else "OFFSETTING" if any(value > 0 for value in numeric)
        else "ADDITIVE"
    )
    a_survived = isolated_a["comparison"]["both_advantages_survived"]
    b_survived = isolated_b["comparison"]["both_advantages_survived"]
    joint_survived = joint["comparison"]["both_advantages_survived"]
    return {
        "factor_a": factor_a,
        "value_a": value_a,
        "factor_b": factor_b,
        "value_b": value_b,
        "classification": classification,
        "interaction_effects": effects,
        "isolated_a_advantage_survived": a_survived,
        "isolated_b_advantage_survived": b_survived,
        "joint_advantage_survived": joint_survived,
        "joint_only_break": a_survived and b_survived and not joint_survived,
        "joint_comparison": joint["comparison"],
        "all_attribution_complete": all(
            row["summary"]["all_attribution_complete"]
            for row in (control, isolated_a, isolated_b, joint)
        ),
    }
