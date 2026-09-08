"""Dual-copy durable-state repair with external anti-rollback anchoring."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4oh_freeze_durability import GENESIS, restore_state

SCHEMA = "phase4oi.dual-copy-repair.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def create_copy(
    copy_id: str, journal: list[dict[str, object]], checkpoint: dict[str, object] | None
) -> dict[str, object]:
    body = {
        "schema": SCHEMA,
        "copy_id": copy_id,
        "journal": copy.deepcopy(journal),
        "checkpoint": copy.deepcopy(checkpoint),
        "safety": _safety(),
    }
    return {**body, "copy_sha256": _digest(body)}


def create_anchor(journal: list[dict[str, object]], *, generation: int) -> dict[str, object]:
    if generation < 1 or generation > len(journal):
        raise ValueError("anchor generation is invalid")
    replay = restore_state(journal[:generation], None)
    if replay["integrity_verdict"] != "PASS":
        raise ValueError("anchor source is invalid")
    body = {
        "schema": SCHEMA,
        "generation": generation,
        "journal_head_sha256": journal[generation - 1]["event_sha256"],
        "state": replay["state"],
        "safety": _safety(),
    }
    return {**body, "anchor_sha256": _digest(body)}


def reconcile_copies(
    copies: list[dict[str, object]], anchor: dict[str, object]
) -> dict[str, object]:
    errors: list[str] = []
    anchor_unsigned = {key: value for key, value in anchor.items() if key != "anchor_sha256"}
    anchor_valid = anchor.get("anchor_sha256") == _digest(anchor_unsigned)
    if not anchor_valid:
        errors.append("ANCHOR_HASH_MISMATCH")
    if anchor.get("schema") != SCHEMA or anchor.get("safety") != _safety():
        errors.append("ANCHOR_INVALID")
    valid = []
    invalid_ids = []
    for durable_copy in copies:
        copy_id = str(durable_copy.get("copy_id"))
        copy_errors = []
        unsigned = {key: value for key, value in durable_copy.items() if key != "copy_sha256"}
        if durable_copy.get("copy_sha256") != _digest(unsigned):
            copy_errors.append("COPY_HASH_MISMATCH")
        if durable_copy.get("schema") != SCHEMA or durable_copy.get("safety") != _safety():
            copy_errors.append("COPY_SCHEMA_OR_SAFETY_INVALID")
        journal = durable_copy.get("journal")
        if not isinstance(journal, list):
            copy_errors.append("COPY_JOURNAL_INVALID")
        else:
            replay = restore_state(journal, durable_copy.get("checkpoint"))
            if replay["integrity_verdict"] != "PASS":
                copy_errors.append("COPY_REPLAY_INVALID")
            generation = anchor.get("generation")
            if not isinstance(generation, int) or len(journal) < generation:
                copy_errors.append("COPY_BEHIND_ANCHOR")
            elif generation > 0 and (
                journal[generation - 1].get("event_sha256") != anchor.get("journal_head_sha256")
            ):
                copy_errors.append("COPY_DIVERGES_FROM_ANCHOR")
            if not copy_errors:
                valid.append((durable_copy, replay))
        if copy_errors:
            invalid_ids.append(copy_id)
    if not anchor_valid:
        valid = []
    if len({str(row.get("copy_id")) for row in copies}) != len(copies):
        errors.append("DUPLICATE_COPY_ID")
    canonical = None
    if valid:
        valid.sort(key=lambda row: (len(row[0]["journal"]), str(row[0]["copy_sha256"])))
        candidate = valid[-1]
        candidate_journal = candidate[0]["journal"]
        for durable_copy, _ in valid[:-1]:
            shorter = durable_copy["journal"]
            if candidate_journal[: len(shorter)] != shorter:
                errors.append("DIVERGENT_VALID_HISTORIES")
        if not errors:
            canonical = candidate
    else:
        errors.append("NO_ANCHORED_VALID_COPY")
    state = canonical[1]["state"] if canonical else "FROZEN"
    repair_plan = []
    if canonical:
        for durable_copy in copies:
            if (
                durable_copy.get("journal") != canonical[0].get("journal")
                or durable_copy.get("checkpoint") != canonical[0].get("checkpoint")
                or str(durable_copy.get("copy_id")) in invalid_ids
            ):
                repair_plan.append(
                    {
                        "target_copy_id": durable_copy.get("copy_id"),
                        "source_copy_sha256": canonical[0]["copy_sha256"],
                        "replacement_journal": copy.deepcopy(canonical[0]["journal"]),
                        "replacement_checkpoint": copy.deepcopy(canonical[0]["checkpoint"]),
                    }
                )
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if canonical else "REFUSE",
        "errors": sorted(set(errors)),
        "anchor_sha256": anchor.get("anchor_sha256"),
        "canonical_copy_sha256": canonical[0]["copy_sha256"] if canonical else None,
        "canonical_journal_head": (
            canonical[0]["journal"][-1]["event_sha256"] if canonical else GENESIS
        ),
        "state": state,
        "capabilities_allowed": bool(canonical and canonical[1]["capabilities_allowed"]),
        "invalid_copy_ids": sorted(invalid_ids),
        "repair_plan": sorted(repair_plan, key=lambda row: str(row["target_copy_id"])),
        "safety": _safety(),
    }
    return {**body, "reconciliation_sha256": _digest(body)}


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
