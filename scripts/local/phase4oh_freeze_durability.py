"""Deterministic emergency-freeze journal, checkpoint, and restart recovery."""

from __future__ import annotations

import hashlib
import json

from scripts.local.phase4oa_aggregate_release_gate import BLOCKED_ON_SETTLEMENT

SCHEMA = "phase4oh.freeze-durability.v1"
GENESIS = "0" * 64


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def append_event(
    journal: list[dict[str, object]],
    *,
    event_type: str,
    evidence_sha256: str,
    evidence_verdict: str,
) -> list[dict[str, object]]:
    if event_type not in {"FREEZE", "RECOVER"}:
        raise ValueError("event type is invalid")
    body = {
        "schema": SCHEMA,
        "sequence": len(journal) + 1,
        "previous_sha256": journal[-1]["event_sha256"] if journal else GENESIS,
        "event_type": event_type,
        "evidence_sha256": evidence_sha256,
        "evidence_verdict": evidence_verdict,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return [*journal, {**body, "event_sha256": _digest(body)}]


def create_checkpoint(journal: list[dict[str, object]]) -> dict[str, object]:
    replay = restore_state(journal, checkpoint=None)
    if replay["integrity_verdict"] != "PASS":
        raise ValueError("cannot checkpoint an invalid journal")
    body = {
        "schema": SCHEMA,
        "sequence": len(journal),
        "head_sha256": journal[-1]["event_sha256"] if journal else GENESIS,
        "state": replay["state"],
        "capabilities_allowed": replay["capabilities_allowed"],
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "checkpoint_sha256": _digest(body)}


def restore_state(
    journal: list[dict[str, object]], checkpoint: dict[str, object] | None
) -> dict[str, object]:
    errors: list[str] = []
    expected_previous = GENESIS
    derived_state = "FROZEN"
    seen = set()
    for index, event in enumerate(journal, start=1):
        if not isinstance(event, dict):
            errors.append("JOURNAL_RECORD_NOT_OBJECT")
            continue
        unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
        event_hash = event.get("event_sha256")
        if event_hash != _digest(unsigned):
            errors.append("JOURNAL_HASH_MISMATCH")
        if event_hash in seen:
            errors.append("JOURNAL_DUPLICATE")
        seen.add(event_hash)
        if event.get("sequence") != index:
            errors.append("JOURNAL_SEQUENCE_GAP_OR_REORDER")
        if event.get("previous_sha256") != expected_previous:
            errors.append("JOURNAL_CHAIN_BROKEN")
        expected_previous = str(event_hash)
        if event.get("schema") != SCHEMA:
            errors.append("JOURNAL_SCHEMA_INVALID")
        if event.get("blocked_on_september_1_settlement") != BLOCKED_ON_SETTLEMENT:
            errors.append("SETTLEMENT_BLOCKER_DRIFT")
        if event.get("safety") != _safety():
            errors.append("SAFETY_INVARIANT_VIOLATION")
        if event.get("event_type") == "FREEZE":
            if event.get("evidence_verdict") != "PASS":
                errors.append("FREEZE_EVIDENCE_INVALID")
            derived_state = "FROZEN"
        elif event.get("event_type") == "RECOVER":
            if event.get("evidence_verdict") != "PASS":
                errors.append("RECOVERY_EVIDENCE_INVALID")
            elif derived_state != "FROZEN":
                errors.append("RECOVERY_WITHOUT_FREEZE")
            else:
                derived_state = "RECOVERED"
        else:
            errors.append("JOURNAL_EVENT_INVALID")
    if not journal:
        errors.append("JOURNAL_MISSING")
    if checkpoint is not None:
        unsigned = {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
        if checkpoint.get("checkpoint_sha256") != _digest(unsigned):
            errors.append("CHECKPOINT_HASH_MISMATCH")
        sequence = checkpoint.get("sequence")
        if not isinstance(sequence, int) or sequence < 0 or sequence > len(journal):
            errors.append("CHECKPOINT_SEQUENCE_INVALID")
        else:
            expected_head = GENESIS if sequence == 0 else journal[sequence - 1].get("event_sha256")
            if checkpoint.get("head_sha256") != expected_head:
                errors.append("CHECKPOINT_HEAD_MISMATCH")
            prefix = restore_state(journal[:sequence], checkpoint=None) if sequence else None
            expected_state = "FROZEN" if prefix is None else prefix["state"]
            if checkpoint.get("state") != expected_state:
                errors.append("CHECKPOINT_STATE_MISMATCH")
        if checkpoint.get("blocked_on_september_1_settlement") != BLOCKED_ON_SETTLEMENT:
            errors.append("SETTLEMENT_BLOCKER_DRIFT")
        if checkpoint.get("safety") != _safety():
            errors.append("SAFETY_INVARIANT_VIOLATION")
    integrity_pass = not errors
    state = derived_state if integrity_pass else "FROZEN"
    allowed = integrity_pass and state == "RECOVERED"
    body = {
        "schema": SCHEMA,
        "integrity_verdict": "PASS" if integrity_pass else "REFUSE",
        "errors": sorted(set(errors)),
        "state": state,
        "capabilities_allowed": allowed,
        "journal_length": len(journal),
        "journal_head_sha256": journal[-1].get("event_sha256") if journal else GENESIS,
        "checkpoint_sha256": checkpoint.get("checkpoint_sha256") if checkpoint else None,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "restoration_sha256": _digest(body)}


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
