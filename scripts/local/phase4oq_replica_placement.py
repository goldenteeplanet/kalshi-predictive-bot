"""Replica placement diversity and correlated-loss resistance proof."""

from __future__ import annotations

import hashlib
import itertools
import json

SCHEMA = "phase4oq.replica-placement.v1"
DIMENSIONS = ("host", "filesystem", "administrator", "failure_domain")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def place_replica(
    *,
    replica_id: str,
    history_sha256: str,
    host: str,
    filesystem: str,
    administrator: str,
    failure_domain: str,
) -> dict[str, object]:
    body = {
        "schema": SCHEMA,
        "replica_id": replica_id,
        "history_sha256": history_sha256,
        "host": host,
        "filesystem": filesystem,
        "administrator": administrator,
        "failure_domain": failure_domain,
        "dependency_sha256": _digest([host, filesystem, administrator, failure_domain]),
        "complete": True,
        "safety": _safety(),
    }
    return {**body, "placement_sha256": _digest(body)}


def audit_placement(
    placements: list[dict[str, object]],
    *,
    trusted_history_sha256: str,
    quorum: int,
    required_multi_losses: list[tuple[tuple[str, str], ...]],
) -> dict[str, object]:
    errors: list[str] = []
    valid = []
    for row in placements:
        unsigned = {key: value for key, value in row.items() if key != "placement_sha256"}
        if row.get("placement_sha256") != _digest(unsigned):
            errors.append("PLACEMENT_HASH_MISMATCH")
        if row.get("schema") != SCHEMA or row.get("safety") != _safety():
            errors.append("PLACEMENT_SCHEMA_OR_SAFETY_INVALID")
        if row.get("history_sha256") != trusted_history_sha256 or row.get("complete") is not True:
            errors.append("REPLICA_INCOMPLETE_OR_WRONG_HISTORY")
        if all(row.get(dimension) for dimension in DIMENSIONS):
            valid.append(row)
        else:
            errors.append("PLACEMENT_DIMENSION_MISSING")
    identities = [row.get("replica_id") for row in placements]
    if len(identities) != len(set(identities)):
        errors.append("REPLICA_ID_REUSED")
    dependencies = [row.get("dependency_sha256") for row in placements]
    if len(dependencies) != len(set(dependencies)):
        errors.append("HIDDEN_DEPENDENCY_COLLISION")
    for dimension in DIMENSIONS:
        values = [row.get(dimension) for row in valid]
        if len(values) != len(set(values)):
            errors.append(f"{dimension.upper()}_NOT_DIVERSE")
    if quorum < 1 or quorum > len(valid):
        errors.append("PLACEMENT_QUORUM_INVALID")
    single_losses = [
        ((dimension, value),)
        for dimension in DIMENSIONS
        for value in sorted({str(row[dimension]) for row in valid})
    ]
    scenarios = single_losses + required_multi_losses
    results = []
    for loss in scenarios:
        lost = {
            row["replica_id"]
            for row in valid
            if any(str(row.get(dimension)) == value for dimension, value in loss)
        }
        survivors = sorted(str(row["replica_id"]) for row in valid if row["replica_id"] not in lost)
        passing = len(survivors) >= quorum
        if not passing:
            errors.append("LOSS_SCENARIO_BREAKS_QUORUM")
        results.append(
            {
                "loss": [list(item) for item in loss],
                "survivors": survivors,
                "survivor_count": len(survivors),
                "recovery_claim_allowed": passing,
            }
        )
    result_keys = [json.dumps(row["loss"], separators=(",", ":")) for row in results]
    if len(result_keys) != len(set(result_keys)):
        errors.append("LOSS_SCENARIO_DUPLICATED")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "trusted_history_sha256": trusted_history_sha256,
        "quorum": quorum,
        "placement_count": len(valid),
        "loss_scenario_count": len(results),
        "results": results,
        "minimum_survivors": min((row["survivor_count"] for row in results), default=0),
        "recovery_claim_allowed": not errors,
        "safety": _safety(),
    }
    return {**body, "audit_sha256": _digest(body)}


def pair_losses(*losses: tuple[str, str]) -> list[tuple[tuple[str, str], ...]]:
    return [tuple(pair) for pair in itertools.combinations(losses, 2)]


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
