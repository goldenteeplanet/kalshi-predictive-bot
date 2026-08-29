"""Deterministic, offline review-history compaction and restoration proof."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4mp_human_review_workflow import validate_workflow

SCHEMA = "phase4mr.review-history-compaction.v1"
ANCHOR_SCHEMA = "phase4mr.review-history-audit-anchor.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def compact_history(
    events: object,
    *,
    retain_tail: int,
    expected_packet_sha256: str,
    expected_implementation_identity_sha256: str,
    evaluated_at: str,
) -> dict[str, object]:
    """Validate a full history and replace its prefix with a content-bound anchor."""
    if not isinstance(events, list) or type(retain_tail) is not int or retain_tail < 0:
        return _refusal("INPUT_INVALID")
    validation = validate_workflow(
        events,
        expected_packet_sha256=expected_packet_sha256,
        expected_implementation_identity_sha256=expected_implementation_identity_sha256,
        evaluated_at=evaluated_at,
    )
    if validation["verdict"] != "PASS":
        return _refusal("SOURCE_HISTORY_INVALID")
    cut = max(0, len(events) - retain_tail)
    prefix = copy.deepcopy(events[:cut])
    tail = copy.deepcopy(events[cut:])
    prefix_head = prefix[-1]["event_sha256"] if prefix else "0" * 64
    anchor_body: dict[str, object] = {
        "schema": ANCHOR_SCHEMA,
        "workflow_id": events[0]["workflow_id"] if events else None,
        "compacted_event_count": cut,
        "prefix_sha256": _digest(prefix),
        "prefix_head_event_sha256": prefix_head,
        "full_history_sha256": _digest(events),
        "full_event_count": len(events),
        "final_state": validation["state"],
        "review_epoch": validation["review_epoch"],
        "final_head_event_sha256": validation["head_event_sha256"],
        "closure_certificate_sha256": (
            _digest(validation["closure_certificate"])
            if validation["closure_certificate"] is not None
            else None
        ),
        "packet_sha256": expected_packet_sha256,
        "implementation_identity_sha256": expected_implementation_identity_sha256,
    }
    anchor = {**anchor_body, "anchor_sha256": _digest(anchor_body)}
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS",
        "errors": [],
        "anchor": anchor,
        "retained_events": tail,
        "retained_tail_sha256": _digest(tail),
        "source_unchanged": _digest(events) == validation["source_sha256"],
        "safety": _safety(),
    }
    result["compaction_sha256"] = _digest(result)
    return result


def validate_compaction(
    artifact: object,
    *,
    expected_anchor_sha256: str,
) -> dict[str, object]:
    """Validate anchor authenticity and retained-tail continuity fail-closed."""
    errors: list[str] = []
    if not isinstance(artifact, dict):
        return _validation_refusal(["ARTIFACT_INVALID"])
    anchor = artifact.get("anchor")
    tail = artifact.get("retained_events")
    if not isinstance(anchor, dict):
        errors.append("ANCHOR_INVALID")
        anchor = {}
    if not isinstance(tail, list):
        errors.append("TAIL_INVALID")
        tail = []
    anchor_body = {key: value for key, value in anchor.items() if key != "anchor_sha256"}
    if anchor.get("schema") != ANCHOR_SCHEMA:
        errors.append("ANCHOR_SCHEMA_INVALID")
    if anchor.get("anchor_sha256") != _digest(anchor_body):
        errors.append("ANCHOR_SELF_HASH_INVALID")
    if anchor.get("anchor_sha256") != expected_anchor_sha256:
        errors.append("ANCHOR_TRUST_MISMATCH")
    if artifact.get("retained_tail_sha256") != _digest(tail):
        errors.append("TAIL_DIGEST_INVALID")
    expected_previous = anchor.get("prefix_head_event_sha256")
    expected_sequence = (
        anchor.get("compacted_event_count", 0) + 1
        if isinstance(anchor.get("compacted_event_count"), int)
        else None
    )
    for index, event in enumerate(tail):
        if not isinstance(event, dict):
            errors.append(f"TAIL_{index}_EVENT_INVALID")
            break
        body = {key: value for key, value in event.items() if key != "event_sha256"}
        if event.get("event_sha256") != _digest(body):
            errors.append(f"TAIL_{index}_EVENT_HASH_INVALID")
        if event.get("previous_event_sha256") != expected_previous:
            errors.append(f"TAIL_{index}_CHAIN_LINK_INVALID")
        if event.get("sequence") != expected_sequence:
            errors.append(f"TAIL_{index}_SEQUENCE_INVALID")
        expected_previous = event.get("event_sha256")
        if expected_sequence is not None:
            expected_sequence += 1
    if anchor.get("full_event_count") != anchor.get("compacted_event_count", 0) + len(tail):
        errors.append("EVENT_COUNT_INVALID")
    if expected_previous != anchor.get("final_head_event_sha256"):
        errors.append("FINAL_HEAD_INVALID")
    return _validation_refusal(sorted(set(errors)), pass_when_empty=True)


def restore_history(
    artifact: object,
    archived_prefix: object,
    *,
    expected_anchor_sha256: str,
    expected_packet_sha256: str,
    expected_implementation_identity_sha256: str,
    evaluated_at: str,
) -> dict[str, object]:
    """Restore and fully revalidate the exact original history."""
    compact_validation = validate_compaction(
        artifact, expected_anchor_sha256=expected_anchor_sha256
    )
    errors = list(compact_validation["errors"])
    if not isinstance(artifact, dict) or not isinstance(archived_prefix, list):
        errors.append("RESTORE_INPUT_INVALID")
        return _restore_result(errors, [])
    anchor = artifact.get("anchor", {})
    if _digest(archived_prefix) != anchor.get("prefix_sha256"):
        errors.append("ARCHIVED_PREFIX_DIGEST_INVALID")
    if len(archived_prefix) != anchor.get("compacted_event_count"):
        errors.append("ARCHIVED_PREFIX_COUNT_INVALID")
    restored = copy.deepcopy(archived_prefix) + copy.deepcopy(artifact.get("retained_events", []))
    if _digest(restored) != anchor.get("full_history_sha256"):
        errors.append("RESTORED_HISTORY_DIGEST_INVALID")
    validation = validate_workflow(
        restored,
        expected_packet_sha256=expected_packet_sha256,
        expected_implementation_identity_sha256=expected_implementation_identity_sha256,
        evaluated_at=evaluated_at,
    )
    if validation["verdict"] != "PASS":
        errors.append("RESTORED_WORKFLOW_INVALID")
    for field, actual in (
        ("final_state", validation["state"]),
        ("review_epoch", validation["review_epoch"]),
        ("final_head_event_sha256", validation["head_event_sha256"]),
    ):
        if anchor.get(field) != actual:
            errors.append(f"RESTORED_{field.upper()}_MISMATCH")
    closure_hash = (
        _digest(validation["closure_certificate"])
        if validation["closure_certificate"] is not None
        else None
    )
    if anchor.get("closure_certificate_sha256") != closure_hash:
        errors.append("RESTORED_CLOSURE_MISMATCH")
    return _restore_result(sorted(set(errors)), restored)


def _safety() -> dict[str, bool]:
    return {
        "simulation_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "service_control": False,
        "order_capability": False,
    }


def _refusal(error: str) -> dict[str, object]:
    result = {"schema": SCHEMA, "verdict": "REFUSE", "errors": [error], "safety": _safety()}
    result["compaction_sha256"] = _digest(result)
    return result


def _validation_refusal(errors: list[str], *, pass_when_empty: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "verdict": "PASS" if pass_when_empty and not errors else "REFUSE",
        "errors": errors,
        "safety": _safety(),
    }
    result["validation_sha256"] = _digest(result)
    return result


def _restore_result(errors: list[str], restored: list[object]) -> dict[str, object]:
    result: dict[str, object] = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "restored_event_count": len(restored),
        "restored_history_sha256": _digest(restored),
        "restored_events": restored,
        "safety": _safety(),
    }
    result["restoration_sha256"] = _digest(result)
    return result
