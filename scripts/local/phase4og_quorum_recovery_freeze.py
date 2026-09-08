"""Fail-closed quorum recovery under partial membership loss."""

from __future__ import annotations

import hashlib
import json

from scripts.local.phase4oa_aggregate_release_gate import BLOCKED_ON_SETTLEMENT
from scripts.local.phase4of_quorum_membership_rotation import create_epoch

SCHEMA = "phase4og.quorum-recovery-freeze.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def create_freeze_certificate(
    epoch: dict[str, object], *, reason: str, observers: set[str]
) -> dict[str, object]:
    errors = []
    members = set(epoch.get("members", []))
    if reason not in {"QUORUM_LOSS", "COMPROMISE_SUSPECTED"}:
        errors.append("FREEZE_REASON_INVALID")
    if not observers or not observers <= members:
        errors.append("FREEZE_OBSERVERS_INVALID")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "state": "FROZEN",
        "epoch_sha256": epoch.get("epoch_sha256"),
        "reason": reason,
        "observers": sorted(observers),
        "time_certification_allowed": False,
        "freshness_renewal_allowed": False,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "freeze_sha256": _digest(body)}


def capability_gate(freeze: object | None) -> dict[str, object]:
    frozen = isinstance(freeze, dict) and freeze.get("state") == "FROZEN"
    valid_hash = False
    if frozen:
        unsigned = {key: value for key, value in freeze.items() if key != "freeze_sha256"}
        valid_hash = freeze.get("freeze_sha256") == _digest(unsigned)
    allowed = not frozen and freeze is None
    errors = [] if allowed else ["RECOVERY_FREEZE_ACTIVE" if valid_hash else "FREEZE_INVALID"]
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if allowed else "REFUSE",
        "errors": errors,
        "time_certification_allowed": allowed,
        "freshness_renewal_allowed": allowed,
        "safety": _safety(),
    }
    return {**body, "gate_sha256": _digest(body)}


def recover_membership(
    epoch: dict[str, object],
    freeze: dict[str, object],
    *,
    next_members: set[str],
    next_quorum: int,
    revoke: set[str],
    recovery_authorities: set[str],
    allowed_recovery_authorities: set[str],
    recovery_quorum: int,
) -> dict[str, object]:
    errors: list[str] = []
    freeze_unsigned = {key: value for key, value in freeze.items() if key != "freeze_sha256"}
    if (
        freeze.get("freeze_sha256") != _digest(freeze_unsigned)
        or freeze.get("verdict") != "PASS"
        or freeze.get("state") != "FROZEN"
        or freeze.get("epoch_sha256") != epoch.get("epoch_sha256")
    ):
        errors.append("VALID_FREEZE_REQUIRED")
    if recovery_quorum <= int(epoch.get("quorum", 0)):
        errors.append("RECOVERY_QUORUM_NOT_STRICTER")
    if recovery_quorum > len(allowed_recovery_authorities):
        errors.append("RECOVERY_POLICY_INVALID")
    if (
        not recovery_authorities <= allowed_recovery_authorities
        or len(recovery_authorities) < recovery_quorum
    ):
        errors.append("RECOVERY_AUTHORITY_QUORUM_MISSING")
    if recovery_authorities & set(epoch.get("members", [])):
        errors.append("RECOVERY_AUTHORITY_NOT_INDEPENDENT")
    cumulative_revoked = set(epoch.get("revoked", [])) | revoke
    if next_members & cumulative_revoked:
        errors.append("REVOKED_WITNESS_REINTRODUCED")
    if not next_members or next_quorum < 1 or next_quorum > len(next_members):
        errors.append("RECOVERED_QUORUM_INVALID")
    next_epoch = None
    if not errors:
        next_epoch = create_epoch(
            epoch=int(epoch["epoch"]) + 1,
            members=next_members,
            quorum=next_quorum,
            revoked=cumulative_revoked,
            parent_epoch_sha256=str(epoch["epoch_sha256"]),
        )
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "freeze_sha256": freeze.get("freeze_sha256"),
        "source_epoch_sha256": epoch.get("epoch_sha256"),
        "recovery_authorities": sorted(recovery_authorities),
        "recovery_quorum": recovery_quorum,
        "revocations": sorted(revoke),
        "next_epoch": next_epoch,
        "state": "RECOVERED" if next_epoch else "FROZEN",
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "recovery_sha256": _digest(body)}


def adjudicate_recoveries(attempts: list[dict[str, object]]) -> dict[str, object]:
    errors = []
    valid = []
    seen = set()
    for attempt in attempts:
        unsigned = {key: value for key, value in attempt.items() if key != "recovery_sha256"}
        if attempt.get("recovery_sha256") != _digest(unsigned):
            errors.append("RECOVERY_HASH_MISMATCH")
        if attempt.get("recovery_sha256") in seen:
            errors.append("RECOVERY_REPLAY")
        seen.add(attempt.get("recovery_sha256"))
        if attempt.get("verdict") == "PASS" and attempt.get("next_epoch"):
            valid.append(attempt)
    children = sorted({row["next_epoch"]["epoch_sha256"] for row in valid})
    if len(children) > 1:
        errors.append("CONFLICTING_RECOVERIES")
    canonical = valid[0] if len(children) == 1 and not errors else None
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if canonical else "REFUSE",
        "errors": sorted(set(errors)),
        "recovery_sha256s": sorted(str(row.get("recovery_sha256")) for row in attempts),
        "child_epoch_sha256s": children,
        "canonical_recovery": canonical,
        "state": "RECOVERED" if canonical else "FROZEN",
        "safety": _safety(),
    }
    return {**body, "adjudication_sha256": _digest(body)}


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
