"""Metamorphic properties for offline adversarial replay."""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from decimal import Decimal

from scripts.local.phase4mz_adversarial_backtest import SCENARIOS
from scripts.local.phase4ni_scenario_coverage import execute_scenario
from scripts.local.phase4nj_reproducibility_bundle import create_bundle, verify_bundle
from scripts.local.phase4nk_portable_replay import canonicalize_artifact

SCHEMA = "phase4nn.metamorphic-replay.v1"
TRANSFORMATIONS = {
    "DEEP_COPY": {"canonical": "SAME", "bundle": "SAME", "replay": "SAME"},
    "KEY_REORDER": {"canonical": "SAME", "bundle": "SAME", "replay": "SAME"},
    "UTC_OFFSET_EQUIVALENT": {"canonical": "SAME", "bundle": "SAME", "replay": "SAME"},
    "UNICODE_EQUIVALENT": {"canonical": "SAME", "bundle": "SAME", "replay": "SAME"},
    "DECIMAL_EQUIVALENT": {"canonical": "SAME", "bundle": "SAME", "replay": "SAME"},
    "STABLE_SCENARIO_REENUMERATION": {"canonical": "SAME", "bundle": "SAME", "replay": "SAME"},
    "PARTITION_REASSEMBLY": {"canonical": "SAME", "bundle": "SAME", "replay": "SAME"},
    "HARMLESS_METADATA_REMOVAL": {"canonical": "CHANGE", "bundle": "CHANGE", "replay": "SAME"},
    "OUTCOME_FLIP": {"canonical": "CHANGE", "bundle": "CHANGE", "replay": "CHANGE"},
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def apply_transformation(records: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    if name not in TRANSFORMATIONS:
        raise ValueError("UNDECLARED_TRANSFORMATION")
    rows = copy.deepcopy(records)
    if name == "KEY_REORDER":
        rows = [dict(reversed(list(row.items()))) for row in rows]
    elif name == "UTC_OFFSET_EQUIVALENT":
        for row in rows:
            for field in ("decision_time", "quote_time", "settlement_time"):
                row[field] = str(row[field]).replace("Z", "+00:00")
            row["feature_times"] = [
                str(value).replace("Z", "+00:00") for value in row["feature_times"]
            ]
    elif name == "UNICODE_EQUIVALENT":
        for row in rows:
            row["ticker"] = unicodedata.normalize("NFD", str(row["ticker"]))
    elif name == "DECIMAL_EQUIVALENT":
        for row in rows:
            for field in ("yes_probability", "yes_ask", "no_ask"):
                row[field] = f"{Decimal(str(row[field])):.4f}"
    elif name == "STABLE_SCENARIO_REENUMERATION":
        assert tuple(list(SCENARIOS)) == SCENARIOS
    elif name == "PARTITION_REASSEMBLY":
        midpoint = len(rows) // 2
        rows = rows[:midpoint] + rows[midpoint:]
    elif name == "HARMLESS_METADATA_REMOVAL":
        for row in rows:
            row.pop("harmless_metadata", None)
    elif name == "OUTCOME_FLIP":
        for row in rows:
            row["outcome"] = "no" if row.get("outcome") == "yes" else "yes"
    return rows


def _observable(records: list[dict[str, object]]) -> dict[str, object]:
    canonical = canonicalize_artifact(records)
    if canonical["verdict"] != "PASS":
        return {"verdict": "REFUSE", "errors": canonical["errors"]}
    bundle = create_bundle(canonical["normalized"], seed=1729)
    verification = verify_bundle(bundle)
    reports = bundle["artifacts"]["expected_outputs"]
    metrics = [
        {
            key: report[key]
            for key in ("scenario", "trade_count", "net_pnl", "max_drawdown", "fill_rate")
        }
        for report in reports
    ]
    refusal_paths = [report["scenario"] for report in reports if report["verdict"] == "REFUSE"]
    return {
        "verdict": verification["verdict"],
        "errors": verification["errors"],
        "canonical": canonical["canonical_sha256"],
        "bundle": bundle["bundle_sha256"],
        "replay": verification["replay_outputs_sha256"],
        "metrics": _digest(metrics),
        "refusal_paths": refusal_paths,
        "scenario_count": len(reports),
        "safety": verification["safety"],
    }


def evaluate_transformation(records: list[dict[str, object]], name: str) -> dict[str, object]:
    errors = []
    if name not in TRANSFORMATIONS:
        return _result(["UNDECLARED_TRANSFORMATION"], name, None, None)
    baseline_records = copy.deepcopy(records)
    if name == "HARMLESS_METADATA_REMOVAL":
        baseline_records = copy.deepcopy(records)
        for row in baseline_records:
            row["harmless_metadata"] = "ignored"
    baseline = _observable(baseline_records)
    transformed_records = apply_transformation(baseline_records, name)
    transformed = _observable(transformed_records)
    declaration = TRANSFORMATIONS[name]
    for layer in ("canonical", "bundle", "replay"):
        equal = baseline.get(layer) == transformed.get(layer)
        if declaration[layer] == "SAME" and not equal:
            errors.append(f"{layer.upper()}_INVARIANT_VIOLATION")
        if declaration[layer] == "CHANGE" and equal:
            errors.append(f"{layer.upper()}_CHANGE_NOT_OBSERVED")
    replay_same = declaration["replay"] == "SAME"
    for layer in ("metrics", "refusal_paths", "verdict"):
        if replay_same and baseline.get(layer) != transformed.get(layer):
            errors.append(f"{layer.upper()}_INVARIANT_VIOLATION")
    if baseline.get("safety") != transformed.get("safety"):
        errors.append("SAFETY_STATE_ALTERED")
    provenance = {
        "transformation": name,
        "input_sha256": _digest(records),
        "transformed_input_sha256": _digest(transformed_records),
        "declaration_sha256": _digest(declaration),
    }
    return _result(sorted(set(errors)), name, baseline, transformed, provenance)


def run_metamorphic_suite(
    records: list[dict[str, object]],
    *,
    joint_scenarios: list[dict[str, object]] | None = None,
    joint_fixtures: dict[str, object] | None = None,
) -> dict[str, object]:
    reports = [evaluate_transformation(records, name) for name in TRANSFORMATIONS]
    errors = (
        ["TRANSFORMATION_INVARIANT_FAILED"]
        if any(row["verdict"] != "PASS" for row in reports)
        else []
    )
    normalized = canonicalize_artifact(records)
    renormalized = canonicalize_artifact(normalized["normalized"])
    if normalized["canonical_sha256"] != renormalized["canonical_sha256"]:
        errors.append("NORMALIZATION_NOT_IDEMPOTENT")
    first_order = apply_transformation(
        apply_transformation(records, "KEY_REORDER"), "UTC_OFFSET_EQUIVALENT"
    )
    second_order = apply_transformation(
        apply_transformation(records, "UTC_OFFSET_EQUIVALENT"), "KEY_REORDER"
    )
    if _observable(first_order) != _observable(second_order):
        errors.append("TRANSFORMATION_ORDER_DEPENDENT")
    joint_hashes = []
    if joint_scenarios is not None:
        if joint_fixtures is None:
            errors.append("JOINT_FIXTURES_MISSING")
        else:
            for scenario in joint_scenarios:
                baseline_fixtures = copy.deepcopy(joint_fixtures)
                baseline_fixtures["backtest_records"] = canonicalize_artifact(records)["normalized"]
                baseline = execute_scenario(scenario, baseline_fixtures)
                fixtures = copy.deepcopy(joint_fixtures)
                fixtures["backtest_records"] = canonicalize_artifact(
                    apply_transformation(records, "KEY_REORDER")
                )["normalized"]
                transformed = execute_scenario(scenario, fixtures)
                if baseline != transformed:
                    errors.append("JOINT_SCENARIO_INVARIANT_VIOLATION")
                joint_hashes.append(baseline["execution_sha256"])
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "transformation_count": len(reports),
        "scenario_count": len(SCENARIOS),
        "joint_scenario_count": len(joint_hashes),
        "reports": reports,
        "joint_execution_hashes": joint_hashes,
        "safety": _safety(),
    }
    result["suite_sha256"] = _digest(result)
    return result


def _result(errors, name, baseline, transformed, provenance=None):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "transformation": name,
        "baseline": baseline,
        "transformed": transformed,
        "provenance": provenance,
        "safety": _safety(),
    }
    result["report_sha256"] = _digest(result)
    return result


def _safety():
    return {
        "offline_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
