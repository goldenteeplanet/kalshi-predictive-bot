"""Independent baseline-lineage audit and quorum replica recovery."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4oo_baseline_lineage import SCHEMA as LINEAGE_SCHEMA

SCHEMA = "phase4op.lineage-recovery-audit.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def create_replica(replica_id: str, entries: list[dict[str, object]]) -> dict[str, object]:
    body = {
        "schema": SCHEMA,
        "replica_id": replica_id,
        "entries": copy.deepcopy(entries),
        "history_sha256": _digest(entries),
        "safety": _safety(),
    }
    return {**body, "replica_sha256": _digest(body)}


def independently_audit_entries(
    entries: object, *, trusted_head_sha256: str, minimum_versions: int
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(entries, list):
        return _result(["ENTRIES_NOT_LIST"], None, 0)
    expected_fields = {
        "schema",
        "version",
        "baseline_sha256",
        "soak_sha256",
        "parent_entry_sha256",
        "promotion_comparison_sha256",
        "performance_claim",
        "safety",
        "entry_sha256",
    }
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            errors.append("ENTRY_FIELDS_INVALID")
            continue
        unsigned = {key: value for key, value in entry.items() if key != "entry_sha256"}
        if entry.get("entry_sha256") != _digest(unsigned):
            errors.append("ENTRY_HASH_MISMATCH")
        if entry.get("schema") != LINEAGE_SCHEMA:
            errors.append("ENTRY_SCHEMA_INVALID")
        if entry.get("version") != index:
            errors.append("MISSING_PREFIX_OR_INTERIOR_VERSION")
        parent = None if index == 1 else entries[index - 2].get("entry_sha256")
        if entry.get("parent_entry_sha256") != parent:
            errors.append("ENTRY_PARENT_MISMATCH")
        promotion = entry.get("promotion_comparison_sha256")
        if (index == 1 and promotion is not None) or (index > 1 and not promotion):
            errors.append("PROMOTION_REFERENCE_INVALID")
        if entry.get("performance_claim") is not True:
            errors.append("ROLLBACK_MASQUERADING_AS_PROMOTION")
        if entry.get("safety") != _lineage_safety():
            errors.append("ENTRY_SAFETY_INVALID")
    head = entries[-1].get("entry_sha256") if entries else None
    if head != trusted_head_sha256:
        errors.append("TRUSTED_HEAD_NOT_PRESERVED")
    if len(entries) < minimum_versions:
        errors.append("INCOMPLETE_RETENTION")
    return _result(sorted(set(errors)), head, len(entries))


def recover_from_replicas(
    replicas: list[dict[str, object]],
    *,
    allowed_replicas: set[str],
    quorum: int,
    trusted_head_sha256: str,
    minimum_versions: int,
) -> dict[str, object]:
    errors: list[str] = []
    if quorum < 1 or quorum > len(allowed_replicas):
        errors.append("REPLICA_QUORUM_POLICY_INVALID")
    identities = [row.get("replica_id") for row in replicas]
    if len(identities) != len(set(identities)):
        errors.append("REPLICA_ID_REPLAY")
    groups: dict[str, list[dict[str, object]]] = {}
    invalid_ids = []
    for replica in replicas:
        replica_id = str(replica.get("replica_id"))
        unsigned = {key: value for key, value in replica.items() if key != "replica_sha256"}
        valid = replica.get("replica_sha256") == _digest(unsigned)
        valid &= replica_id in allowed_replicas
        valid &= replica.get("schema") == SCHEMA and replica.get("safety") == _safety()
        entries = replica.get("entries")
        valid &= replica.get("history_sha256") == _digest(entries)
        audit = independently_audit_entries(
            entries, trusted_head_sha256=trusted_head_sha256, minimum_versions=minimum_versions
        )
        valid &= audit["verdict"] == "PASS"
        if valid:
            groups.setdefault(str(replica["history_sha256"]), []).append(replica)
        else:
            invalid_ids.append(replica_id)
    qualified = [
        (history_hash, rows) for history_hash, rows in groups.items() if len(rows) >= quorum
    ]
    if len(qualified) != 1:
        errors.append("UNIQUE_REPLICA_QUORUM_NOT_MET")
    canonical_entries = (
        copy.deepcopy(qualified[0][1][0]["entries"]) if len(qualified) == 1 else None
    )
    repair_plan = []
    if canonical_entries is not None and not errors:
        for replica in replicas:
            if replica.get("entries") != canonical_entries:
                repair_plan.append(
                    {
                        "target_replica_id": replica.get("replica_id"),
                        "replacement_entries": copy.deepcopy(canonical_entries),
                        "history_sha256": qualified[0][0],
                    }
                )
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if canonical_entries is not None and not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "trusted_head_sha256": trusted_head_sha256,
        "canonical_history_sha256": qualified[0][0] if len(qualified) == 1 else None,
        "canonical_entries": canonical_entries if not errors else None,
        "invalid_replica_ids": sorted(invalid_ids),
        "repair_plan": sorted(repair_plan, key=lambda row: str(row["target_replica_id"])),
        "state": "FROZEN",
        "capabilities_allowed": False,
        "independent_of_primary_verifier": True,
        "safety": _safety(),
    }
    return {**body, "recovery_sha256": _digest(body)}


def _result(errors, head, count):
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "head_sha256": head,
        "version_count": count,
        "safety": _safety(),
    }
    return {**body, "audit_sha256": _digest(body)}


def _lineage_safety() -> dict[str, bool]:
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


def _safety() -> dict[str, bool]:
    return _lineage_safety()
