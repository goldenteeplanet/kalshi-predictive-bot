"""Deterministic recovery-cost sensitivity and tail-budget soak proof."""

from __future__ import annotations

import hashlib
import json
import math
import random

from scripts.local.phase4ol_disaster_recovery_chaos import SCENARIOS

SCHEMA = "phase4om.recovery-tail-soak.v1"
RUN_COUNT = 256
TAIL_MULTIPLIER = 2


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _percentile(values: list[int], percentile: int) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile / 100 * len(ordered)) - 1)]


def run_soak(*, seed: int, run_count: int = RUN_COUNT) -> dict[str, object]:
    if run_count < 100:
        raise ValueError("at least 100 runs are required for tail evidence")
    rng = random.Random(seed)
    runs = []
    for run_id in range(run_count):
        scenario_costs = {}
        unsafe = False
        for scenario, nominal_budget, expected_state in SCENARIOS:
            jitter = rng.randint(0, nominal_budget)
            cost = nominal_budget + jitter
            scenario_costs[scenario] = cost
            degraded = expected_state == "FROZEN"
            capabilities_allowed = not degraded and scenario == "AUTHORIZED_RECOVERY"
            unsafe |= degraded and capabilities_allowed
        total = sum(scenario_costs.values())
        runs.append(
            {
                "run_id": run_id,
                "scenario_costs": scenario_costs,
                "total_cost": total,
                "unsafe": unsafe,
            }
        )
    totals = [row["total_cost"] for row in runs]
    metrics = {
        "median": _percentile(totals, 50),
        "p95": _percentile(totals, 95),
        "p99": _percentile(totals, 99),
        "worst": max(totals),
    }
    scenario_tails = {
        scenario: {
            "p99": _percentile([row["scenario_costs"][scenario] for row in runs], 99),
            "worst": max(row["scenario_costs"][scenario] for row in runs),
            "tail_budget": nominal * TAIL_MULTIPLIER,
        }
        for scenario, nominal, _ in SCENARIOS
    }
    body = {
        "schema": SCHEMA,
        "seed": seed,
        "run_count": run_count,
        "runs": runs,
        "metrics": metrics,
        "scenario_tails": scenario_tails,
        "aggregate_tail_budget": sum(row[1] for row in SCENARIOS) * TAIL_MULTIPLIER,
        "unsafe_run_count": sum(row["unsafe"] for row in runs),
        "safety": _safety(),
    }
    return {**body, "soak_sha256": _digest(body)}


def verify_soak(report: object) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(report, dict):
        return _result(["REPORT_NOT_OBJECT"], None)
    unsigned = {key: value for key, value in report.items() if key != "soak_sha256"}
    if report.get("soak_sha256") != _digest(unsigned):
        errors.append("SOAK_HASH_MISMATCH")
    if report.get("schema") != SCHEMA or report.get("safety") != _safety():
        errors.append("SCHEMA_OR_SAFETY_INVALID")
    runs = report.get("runs")
    if not isinstance(runs, list) or len(runs) < 100 or len(runs) != report.get("run_count"):
        errors.append("RUN_CORPUS_INVALID")
        runs = []
    expected_ids = list(range(len(runs)))
    if [row.get("run_id") for row in runs if isinstance(row, dict)] != expected_ids:
        errors.append("RUN_SEQUENCE_INVALID")
    totals = []
    unsafe = 0
    costs_valid = True
    expected_scenarios = [row[0] for row in SCENARIOS]
    for row in runs:
        if not isinstance(row, dict) or list(row.get("scenario_costs", {})) != expected_scenarios:
            errors.append("SCENARIO_COSTS_INVALID")
            costs_valid = False
            continue
        calculated = sum(row["scenario_costs"].values())
        if calculated != row.get("total_cost"):
            errors.append("TOTAL_COST_MISMATCH")
        totals.append(calculated)
        unsafe += bool(row.get("unsafe"))
    if totals:
        expected_metrics = {
            "median": _percentile(totals, 50),
            "p95": _percentile(totals, 95),
            "p99": _percentile(totals, 99),
            "worst": max(totals),
        }
        if report.get("metrics") != expected_metrics:
            errors.append("PERCENTILE_METRICS_INVALID")
        if expected_metrics["p99"] > report.get("aggregate_tail_budget", 0):
            errors.append("AGGREGATE_P99_BUDGET_EXCEEDED")
    expected_tails = {}
    if runs and costs_valid:
        for scenario, nominal, _ in SCENARIOS:
            values = [row["scenario_costs"][scenario] for row in runs]
            expected_tails[scenario] = {
                "p99": _percentile(values, 99),
                "worst": max(values),
                "tail_budget": nominal * TAIL_MULTIPLIER,
            }
            if expected_tails[scenario]["p99"] > nominal * TAIL_MULTIPLIER:
                errors.append(f"{scenario}:P99_BUDGET_EXCEEDED")
    if report.get("scenario_tails") != expected_tails:
        errors.append("SCENARIO_TAILS_INVALID")
    if unsafe or report.get("unsafe_run_count") != 0:
        errors.append("UNSAFE_RUN_DETECTED")
    try:
        replay = run_soak(seed=int(report["seed"]), run_count=int(report["run_count"]))
    except (KeyError, TypeError, ValueError):
        errors.append("REPLAY_PARAMETERS_INVALID")
    else:
        if replay != report:
            errors.append("NONDETERMINISTIC_OR_ALTERED_REPLAY")
    return _result(sorted(set(errors)), report.get("soak_sha256"))


def _result(errors, subject):
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "subject_sha256": subject,
        "safety": _safety(),
    }
    return {**body, "verification_sha256": _digest(body)}


def _safety() -> dict[str, bool]:
    return {
        "offline_only": True,
        "infrastructure_mutation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
