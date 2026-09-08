"""Trusted-time quorum membership rotation and compromise containment."""

from __future__ import annotations

import hashlib
import json

from scripts.local.phase4oa_aggregate_release_gate import BLOCKED_ON_SETTLEMENT

SCHEMA = "phase4of.quorum-membership-rotation.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def create_epoch(
    *,
    epoch: int,
    members: set[str],
    quorum: int,
    revoked: set[str],
    parent_epoch_sha256: str | None,
) -> dict[str, object]:
    if epoch < 1 or (epoch == 1) != (parent_epoch_sha256 is None):
        raise ValueError("epoch and parent are inconsistent")
    if not members or quorum < 1 or quorum > len(members):
        raise ValueError("membership quorum is invalid")
    if members & revoked:
        raise ValueError("revoked witness cannot be active")
    body = {
        "schema": SCHEMA,
        "epoch": epoch,
        "members": sorted(members),
        "quorum": quorum,
        "revoked": sorted(revoked),
        "parent_epoch_sha256": parent_epoch_sha256,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "epoch_sha256": _digest(body)}


def propose_rotation(
    current: dict[str, object],
    *,
    next_members: set[str],
    next_quorum: int,
    revoke: set[str],
    old_approvers: set[str],
    new_approvers: set[str],
) -> dict[str, object]:
    cumulative_revoked = set(current.get("revoked", [])) | revoke
    errors = []
    current_members = set(current.get("members", []))
    if revoke - current_members:
        errors.append("REVOCATION_TARGET_NOT_ACTIVE")
    if next_members & cumulative_revoked:
        errors.append("REVOKED_WITNESS_REINTRODUCED")
    if not next_members or next_quorum < 1 or next_quorum > len(next_members):
        errors.append("NEXT_QUORUM_INVALID")
    if not old_approvers <= current_members or len(old_approvers) < int(current.get("quorum", 0)):
        errors.append("OLD_QUORUM_APPROVAL_MISSING")
    if not new_approvers <= next_members or len(new_approvers) < next_quorum:
        errors.append("NEW_QUORUM_APPROVAL_MISSING")
    continuing = current_members & next_members
    if not continuing or not (old_approvers & new_approvers & continuing):
        errors.append("QUORUM_OVERLAP_MISSING")
    next_epoch = None
    if not errors:
        next_epoch = create_epoch(
            epoch=int(current["epoch"]) + 1,
            members=next_members,
            quorum=next_quorum,
            revoked=cumulative_revoked,
            parent_epoch_sha256=str(current["epoch_sha256"]),
        )
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "current_epoch_sha256": current.get("epoch_sha256"),
        "old_approvers": sorted(old_approvers),
        "new_approvers": sorted(new_approvers),
        "revocations": sorted(revoke),
        "next_epoch": next_epoch,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "rotation_sha256": _digest(body)}


def verify_rotation_chain(
    epochs: list[dict[str, object]], rotations: list[dict[str, object]]
) -> dict[str, object]:
    errors: list[str] = []
    if not epochs or len(rotations) != len(epochs) - 1:
        errors.append("CHAIN_CARDINALITY_INVALID")
    seen_hashes: set[object] = set()
    permanently_revoked: set[str] = set()
    for index, epoch in enumerate(epochs):
        unsigned = {key: value for key, value in epoch.items() if key != "epoch_sha256"}
        if epoch.get("epoch_sha256") != _digest(unsigned):
            errors.append("EPOCH_HASH_MISMATCH")
        if epoch.get("epoch_sha256") in seen_hashes:
            errors.append("EPOCH_REPLAY")
        seen_hashes.add(epoch.get("epoch_sha256"))
        if epoch.get("epoch") != index + 1:
            errors.append("EPOCH_ROLLBACK_OR_GAP")
        expected_parent = None if index == 0 else epochs[index - 1].get("epoch_sha256")
        if epoch.get("parent_epoch_sha256") != expected_parent:
            errors.append("EPOCH_PARENT_MISMATCH")
        members = set(epoch.get("members", []))
        revoked = set(epoch.get("revoked", []))
        if not permanently_revoked <= revoked:
            errors.append("REVOCATION_HISTORY_LOST")
        permanently_revoked |= revoked
        if members & permanently_revoked:
            errors.append("REVOKED_WITNESS_ACTIVE")
        if epoch.get("blocked_on_september_1_settlement") != BLOCKED_ON_SETTLEMENT:
            errors.append("SETTLEMENT_BLOCKER_DRIFT")
        if epoch.get("safety") != _safety():
            errors.append("SAFETY_INVARIANT_VIOLATION")
        if index:
            rotation = rotations[index - 1]
            unsigned_rotation = {
                key: value for key, value in rotation.items() if key != "rotation_sha256"
            }
            if rotation.get("rotation_sha256") != _digest(unsigned_rotation):
                errors.append("ROTATION_HASH_MISMATCH")
            if rotation.get("verdict") != "PASS" or rotation.get("next_epoch") != epoch:
                errors.append("ROTATION_EPOCH_BINDING_INVALID")
            if rotation.get("current_epoch_sha256") != expected_parent:
                errors.append("ROTATION_PARENT_MISMATCH")
    competing = {}
    for rotation in rotations:
        parent = rotation.get("current_epoch_sha256")
        child = (rotation.get("next_epoch") or {}).get("epoch_sha256")
        competing.setdefault(parent, set()).add(child)
    if any(len(children) > 1 for children in competing.values()):
        errors.append("CONFLICTING_ROTATIONS")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "epoch_sha256s": [epoch.get("epoch_sha256") for epoch in epochs],
        "rotation_sha256s": [rotation.get("rotation_sha256") for rotation in rotations],
        "active_members": epochs[-1].get("members", []) if epochs else [],
        "revoked": sorted(permanently_revoked),
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
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
