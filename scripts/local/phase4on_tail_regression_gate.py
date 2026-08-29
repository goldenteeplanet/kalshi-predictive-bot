"""Versioned recovery-tail baselines and bounded regression detection."""

from __future__ import annotations

import hashlib
import json

from scripts.local.phase4om_recovery_tail_soak import verify_soak

SCHEMA = "phase4on.tail-regression-gate.v1"
METRICS = ("p95", "p99", "worst")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def create_baseline(report: dict[str, object], *, version: int) -> dict[str, object]:
    verification = verify_soak(report)
    if verification["verdict"] != "PASS" or version < 1:
        raise ValueError("baseline source or version is invalid")
    body = {
        "schema": SCHEMA,
        "version": version,
        "soak_sha256": report["soak_sha256"],
        "seed": report["seed"],
        "run_count": report["run_count"],
        "metrics": {key: report["metrics"][key] for key in METRICS},
        "aggregate_tail_budget": report["aggregate_tail_budget"],
        "unsafe_run_count": report["unsafe_run_count"],
        "safety": _safety(),
    }
    return {**body, "baseline_sha256": _digest(body)}


def compare_candidate(
    baseline: dict[str, object],
    candidate: dict[str, object],
    *,
    trusted_baseline_sha256: str,
    absolute_tolerance: int,
    relative_tolerance: float,
) -> dict[str, object]:
    errors: list[str] = []
    unsigned = {key: value for key, value in baseline.items() if key != "baseline_sha256"}
    if baseline.get("baseline_sha256") != _digest(unsigned):
        errors.append("BASELINE_HASH_MISMATCH")
    if baseline.get("baseline_sha256") != trusted_baseline_sha256:
        errors.append("TRUSTED_BASELINE_MISMATCH")
    if baseline.get("schema") != SCHEMA or baseline.get("safety") != _safety():
        errors.append("BASELINE_SCHEMA_OR_SAFETY_INVALID")
    if absolute_tolerance < 0 or not 0 <= relative_tolerance <= 1:
        errors.append("TOLERANCE_POLICY_INVALID")
    candidate_verification = verify_soak(candidate)
    if candidate_verification["verdict"] != "PASS":
        errors.append("CANDIDATE_SOAK_INVALID")
    if candidate.get("run_count") != baseline.get("run_count"):
        errors.append("SAMPLE_SIZE_CHANGED")
    if candidate.get("seed") == baseline.get("seed"):
        errors.append("CANDIDATE_SEED_REUSED")
    if candidate.get("unsafe_run_count") != 0:
        errors.append("UNSAFE_RUN_PRESENT")
    if candidate.get("metrics", {}).get("p99", 10**9) > candidate.get("aggregate_tail_budget", -1):
        errors.append("CANDIDATE_TAIL_BUDGET_VIOLATION")
    deltas = {}
    classifications = {}
    for metric in METRICS:
        old = baseline.get("metrics", {}).get(metric)
        new = candidate.get("metrics", {}).get(metric)
        if not isinstance(old, int) or not isinstance(new, int):
            errors.append("METRIC_TYPE_INVALID")
            continue
        absolute_delta = new - old
        relative_delta = absolute_delta / old if old else float("inf")
        allowed = max(absolute_tolerance, old * relative_tolerance)
        deltas[metric] = {
            "baseline": old,
            "candidate": new,
            "absolute": absolute_delta,
            "relative": relative_delta,
            "allowed_increase": allowed,
        }
        if absolute_delta < 0:
            classifications[metric] = "IMPROVEMENT"
        elif absolute_delta <= allowed:
            classifications[metric] = "WITHIN_TOLERANCE"
        else:
            classifications[metric] = "REGRESSION"
            errors.append(f"{metric.upper()}_REGRESSION")
    promotable = not errors
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if promotable else "REFUSE",
        "errors": sorted(set(errors)),
        "baseline_sha256": baseline.get("baseline_sha256"),
        "candidate_soak_sha256": candidate.get("soak_sha256"),
        "candidate_verification_sha256": candidate_verification["verification_sha256"],
        "deltas": deltas,
        "classifications": classifications,
        "promotion_allowed": promotable,
        "next_baseline_version": baseline.get("version", 0) + 1 if promotable else None,
        "safety": _safety(),
    }
    return {**body, "comparison_sha256": _digest(body)}


def promote_baseline(
    comparison: dict[str, object], candidate: dict[str, object]
) -> dict[str, object]:
    if comparison.get("verdict") != "PASS" or comparison.get("promotion_allowed") is not True:
        raise ValueError("candidate is not eligible for promotion")
    if comparison.get("candidate_soak_sha256") != candidate.get("soak_sha256"):
        raise ValueError("candidate binding mismatch")
    return create_baseline(candidate, version=int(comparison["next_baseline_version"]))


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
