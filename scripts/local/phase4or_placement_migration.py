"""Staged replica placement migration and continuous diversity-drift detection."""

from __future__ import annotations

import hashlib
import json

from scripts.local.phase4oq_replica_placement import audit_placement

SCHEMA = "phase4or.placement-migration.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def conduct_migration(
    source: list[dict[str, object]],
    destination: dict[str, object],
    *,
    remove_replica_id: str,
    trusted_history_sha256: str,
    quorum: int,
    required_multi_losses: list[tuple[tuple[str, str], ...]],
    approvers: set[str],
    allowed_approvers: set[str],
) -> dict[str, object]:
    errors: list[str] = []
    if len(approvers) < 2 or not approvers <= allowed_approvers:
        errors.append("INDEPENDENT_APPROVAL_MISSING")
    source_ids = [row.get("replica_id") for row in source]
    if remove_replica_id not in source_ids:
        errors.append("SOURCE_REPLICA_NOT_FOUND")
    if destination.get("replica_id") in source_ids:
        errors.append("DESTINATION_ID_ALREADY_ACTIVE")
    destination_unsigned = {
        key: value for key, value in destination.items() if key != "placement_sha256"
    }
    if destination.get("placement_sha256") != _digest(destination_unsigned):
        errors.append("DESTINATION_HASH_INVALID")
    stages = [
        ("SOURCE", list(source)),
        ("ADD_DESTINATION", [*source, destination]),
        (
            "REMOVE_SOURCE",
            [row for row in [*source, destination] if row.get("replica_id") != remove_replica_id],
        ),
    ]
    stage_results = []
    for stage_name, placements in stages:
        audit = audit_placement(
            placements,
            trusted_history_sha256=trusted_history_sha256,
            quorum=quorum,
            required_multi_losses=required_multi_losses,
        )
        if audit["verdict"] != "PASS":
            errors.append(f"{stage_name}_DIVERSITY_OR_SURVIVABILITY_FAILED")
        stage_results.append(
            {
                "stage": stage_name,
                "placement_sha256s": sorted(str(row.get("placement_sha256")) for row in placements),
                "audit_sha256": audit["audit_sha256"],
                "verdict": audit["verdict"],
                "minimum_survivors": audit["minimum_survivors"],
            }
        )
    final_placements = stages[-1][1] if not errors else None
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "source_set_sha256": _digest(source),
        "destination_placement_sha256": destination.get("placement_sha256"),
        "remove_replica_id": remove_replica_id,
        "approvers": sorted(approvers),
        "stages": stage_results,
        "final_placements": final_placements,
        "recovery_claim_allowed": not errors,
        "safety": _safety(),
    }
    return {**body, "ceremony_sha256": _digest(body)}


def detect_diversity_drift(
    ceremony: dict[str, object],
    current: list[dict[str, object]],
    *,
    trusted_history_sha256: str,
    quorum: int,
    required_multi_losses: list[tuple[tuple[str, str], ...]],
) -> dict[str, object]:
    errors = []
    ceremony_unsigned = {key: value for key, value in ceremony.items() if key != "ceremony_sha256"}
    if ceremony.get("ceremony_sha256") != _digest(ceremony_unsigned):
        errors.append("CEREMONY_HASH_MISMATCH")
    if ceremony.get("verdict") != "PASS":
        errors.append("CEREMONY_NOT_PASSING")
    certified = ceremony.get("final_placements")
    if not isinstance(certified, list):
        certified = []
        errors.append("CERTIFIED_PLACEMENT_MISSING")
    expected = sorted(str(row.get("placement_sha256")) for row in certified)
    observed = sorted(str(row.get("placement_sha256")) for row in current)
    certified_state_sha256 = _digest(sorted(certified, key=lambda row: str(row.get("replica_id"))))
    observed_state_sha256 = _digest(sorted(current, key=lambda row: str(row.get("replica_id"))))
    if observed != expected or observed_state_sha256 != certified_state_sha256:
        errors.append("PLACEMENT_SET_DRIFT")
    audit = audit_placement(
        current,
        trusted_history_sha256=trusted_history_sha256,
        quorum=quorum,
        required_multi_losses=required_multi_losses,
    )
    if audit["verdict"] != "PASS":
        errors.append("CONTINUOUS_DIVERSITY_AUDIT_FAILED")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "ceremony_sha256": ceremony.get("ceremony_sha256"),
        "certified_placement_sha256s": expected,
        "observed_placement_sha256s": observed,
        "certified_state_sha256": certified_state_sha256,
        "observed_state_sha256": observed_state_sha256,
        "continuous_audit_sha256": audit["audit_sha256"],
        "recovery_claim_allowed": not errors,
        "state": "READY" if not errors else "FROZEN",
        "safety": _safety(),
    }
    return {**body, "drift_report_sha256": _digest(body)}


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
