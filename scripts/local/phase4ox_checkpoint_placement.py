"""Checkpoint replica placement and correlated interruption recovery proof."""

from __future__ import annotations

import hashlib
import json

from scripts.local.phase4ow_checkpoint_redundancy import create_replica, recover_replicas

SCHEMA = "phase4ox.checkpoint-placement.v1"
DIMENSIONS = (
    "host",
    "filesystem",
    "administrator",
    "power_domain",
    "runtime_domain",
    "failure_domain",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def place_checkpoint_replica(replica: dict[str, object], **placement: str) -> dict[str, object]:
    if set(placement) != set(DIMENSIONS) or not all(placement.values()):
        raise ValueError("all placement dimensions are required")
    body = {
        "schema": SCHEMA,
        "replica": replica,
        "placement": {dimension: placement[dimension] for dimension in DIMENSIONS},
        "dependency_sha256": _digest([placement[dimension] for dimension in DIMENSIONS]),
        "safety": _safety(),
    }
    return {**body, "placed_replica_sha256": _digest(body)}


def certify_interruption_matrix(
    original_records: list[dict[str, object]],
    checkpoints: list[dict[str, object]],
    placements: list[dict[str, object]],
    scenarios: list[dict[str, object]],
    *,
    allowed_replicas: set[str],
    quorum: int,
) -> dict[str, object]:
    errors: list[str] = []
    dependencies = []
    for placed in placements:
        unsigned = {key: value for key, value in placed.items() if key != "placed_replica_sha256"}
        if placed.get("placed_replica_sha256") != _digest(unsigned):
            errors.append("PLACED_REPLICA_HASH_MISMATCH")
        if placed.get("schema") != SCHEMA or placed.get("safety") != _safety():
            errors.append("PLACEMENT_SCHEMA_OR_SAFETY_INVALID")
        dependencies.append(placed.get("dependency_sha256"))
    if len(dependencies) != len(set(dependencies)):
        errors.append("HIDDEN_DEPENDENCY_COLLISION")
    for dimension in DIMENSIONS:
        values = [placed.get("placement", {}).get(dimension) for placed in placements]
        if len(values) != len(set(values)):
            errors.append(f"{dimension.upper()}_NOT_DIVERSE")
    rows = []
    convergence_hashes = set()
    for prefix_index in range(len(checkpoints)):
        prefix = checkpoints[: prefix_index + 1]
        for scenario in scenarios:
            losses = scenario.get("losses", [])
            survivors = []
            for placed in placements:
                location = placed.get("placement", {})
                lost = any(location.get(dimension) == value for dimension, value in losses)
                if not lost:
                    replica_id = str(placed["replica"]["replica_id"])
                    survivors.append(create_replica(replica_id, prefix))
            recovery = recover_replicas(
                original_records,
                survivors,
                allowed_replicas=allowed_replicas,
                quorum=quorum,
            )
            expected_survivable = scenario.get("survivable") is True
            if expected_survivable and recovery["verdict"] != "PASS":
                errors.append("SURVIVABLE_SCENARIO_FAILED")
            if not expected_survivable and recovery["verdict"] != "REFUSE":
                errors.append("UNSURVIVABLE_SCENARIO_CLAIMED_RECOVERY")
            if recovery["verdict"] == "PASS":
                convergence_hashes.add(recovery["converged_orchestration_sha256"])
            rows.append(
                {
                    "prefix_index": prefix_index,
                    "scenario": scenario.get("name"),
                    "survivor_count": len(survivors),
                    "expected_survivable": expected_survivable,
                    "recovery_verdict": recovery["verdict"],
                    "recovery_sha256": recovery["recovery_sha256"],
                    "converged_orchestration_sha256": recovery.get(
                        "converged_orchestration_sha256"
                    ),
                }
            )
    if len(convergence_hashes) != 1:
        errors.append("SURVIVABLE_SCENARIOS_DID_NOT_CONVERGE")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "prefix_count": len(checkpoints),
        "scenario_count": len(scenarios),
        "case_count": len(rows),
        "results": rows,
        "converged_orchestration_sha256": (
            next(iter(convergence_hashes)) if len(convergence_hashes) == 1 else None
        ),
        "settlement_record_unchanged": not errors,
        "recovery_claim_allowed": not errors,
        "executable": False,
        "safety": _safety(),
    }
    return {**body, "matrix_sha256": _digest(body)}


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
