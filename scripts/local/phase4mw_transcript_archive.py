"""In-memory transcript retention, legal-hold, and tombstone simulation."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime

SCHEMA = "phase4mw.transcript-archive.v1"
EVENT_SCHEMA = "phase4mw.archive-custody-event.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        value_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value_time.astimezone(UTC) if value_time.tzinfo else None


def create_entry(
    *,
    transcript_sha256: str,
    policy_generation: int,
    created_at: str,
    retention_class: str,
    expires_at: str,
    custodian: str,
) -> dict[str, object]:
    body: dict[str, object] = {
        "transcript_sha256": transcript_sha256,
        "policy_generation": policy_generation,
        "created_at": created_at,
        "retention_class": retention_class,
        "expires_at": expires_at,
    }
    entry = {**body, "entry_sha256": _digest(body)}
    return {
        "schema": SCHEMA,
        "entry": entry,
        "events": [make_event([], "ARCHIVE", custodian, created_at, {})],
    }


def make_event(
    existing: list[dict[str, object]],
    operation: str,
    actor: str,
    occurred_at: str,
    payload: dict[str, object],
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": EVENT_SCHEMA,
        "sequence": len(existing) + 1,
        "operation": operation,
        "actor": actor,
        "occurred_at": occurred_at,
        "payload": payload,
        "previous_event_sha256": existing[-1]["event_sha256"] if existing else "0" * 64,
    }
    return {**body, "event_sha256": _digest(body)}


def append_event(
    archive: dict[str, object], operation: str, actor: str, occurred_at: str, payload=None
) -> dict[str, object]:
    result = copy.deepcopy(archive)
    events = result.get("events", [])
    events.append(make_event(events, operation, actor, occurred_at, payload or {}))
    return result


def validate_archive(archive: object, *, evaluated_at: str) -> dict[str, object]:
    errors: list[str] = []
    now = _time(evaluated_at)
    if (
        not isinstance(archive, dict)
        or not isinstance(archive.get("entry"), dict)
        or not isinstance(archive.get("events"), list)
        or now is None
    ):
        return _result(["INPUT_INVALID"], "INVALID", 0)
    entry, events = archive["entry"], archive["events"]
    entry_body = {key: value for key, value in entry.items() if key != "entry_sha256"}
    if entry.get("entry_sha256") != _digest(entry_body):
        errors.append("ENTRY_HASH_INVALID")
    created, expires = _time(entry.get("created_at")), _time(entry.get("expires_at"))
    if created is None or expires is None or expires <= created:
        errors.append("RETENTION_WINDOW_INVALID")
    head = "0" * 64
    previous_time = None
    holds: set[str] = set()
    tombstoned = False
    deleted_digest = None
    for index, event in enumerate(events):
        row_errors: list[str] = []
        if not isinstance(event, dict):
            errors.append(f"EVENT_{index}_INVALID")
            break
        body = {key: value for key, value in event.items() if key != "event_sha256"}
        if event.get("event_sha256") != _digest(body):
            row_errors.append("HASH_INVALID")
        if event.get("previous_event_sha256") != head or event.get("sequence") != index + 1:
            row_errors.append("CHAIN_OR_SEQUENCE_INVALID")
        occurred = _time(event.get("occurred_at"))
        if occurred is None or (previous_time is not None and occurred < previous_time):
            row_errors.append("TIME_INVALID_OR_ROLLBACK")
        operation, payload = event.get("operation"), event.get("payload")
        if not isinstance(payload, dict):
            row_errors.append("PAYLOAD_INVALID")
            payload = {}
        if index == 0 and operation != "ARCHIVE":
            row_errors.append("FIRST_EVENT_NOT_ARCHIVE")
        elif operation == "HOLD":
            hold_id = payload.get("hold_id")
            if tombstoned or not isinstance(hold_id, str) or not hold_id or hold_id in holds:
                row_errors.append("HOLD_INVALID")
            else:
                holds.add(hold_id)
        elif operation == "RELEASE":
            hold_id = payload.get("hold_id")
            if tombstoned or hold_id not in holds:
                row_errors.append("HOLD_RELEASE_INVALID")
            else:
                holds.remove(hold_id)
        elif operation == "DELETE":
            if tombstoned:
                row_errors.append("DUPLICATE_TOMBSTONE")
            if holds:
                row_errors.append("LEGAL_HOLD_ACTIVE")
            if expires is None or occurred is None or occurred < expires:
                row_errors.append("RETENTION_NOT_EXPIRED")
            if payload.get("deleted_entry_sha256") != entry.get("entry_sha256"):
                row_errors.append("TOMBSTONE_BINDING_INVALID")
            if not row_errors:
                tombstoned = True
                deleted_digest = payload.get("deleted_entry_sha256")
        elif operation not in {"ARCHIVE", "TRANSFER"}:
            row_errors.append("OPERATION_INVALID")
        if tombstoned and operation != "DELETE" and index > 0:
            row_errors.append("POST_TOMBSTONE_MUTATION")
        if row_errors:
            errors.extend(f"EVENT_{index}_{error}" for error in sorted(set(row_errors)))
            break
        head = str(event.get("event_sha256"))
        previous_time = occurred
    state = "TOMBSTONED" if tombstoned else ("HELD" if holds else "RETAINED")
    result = _result(sorted(set(errors)), state, len(events))
    result.update(
        {
            "head_event_sha256": head,
            "active_hold_ids": sorted(holds),
            "deleted_entry_sha256": deleted_digest,
        }
    )
    result["audit_sha256"] = _digest(result)
    return result


def simulate_hold_delete_race(
    archive: dict[str, object],
    hold_event: dict[str, object],
    delete_event: dict[str, object],
    *,
    evaluated_at: str,
) -> dict[str, object]:
    base = copy.deepcopy(archive)
    outcomes = []
    for candidate in (hold_event, delete_event):
        trial = copy.deepcopy(base)
        trial["events"].append(copy.deepcopy(candidate))
        validation = validate_archive(trial, evaluated_at=evaluated_at)
        if validation["verdict"] == "PASS":
            base = trial
            outcomes.append("ACCEPTED")
        else:
            outcomes.append("REFUSED")
    result = {
        "verdict": "PASS",
        "outcomes": outcomes,
        "accepted_count": outcomes.count("ACCEPTED"),
        "final_archive": base,
        "safety": _safety(),
    }
    result["race_sha256"] = _digest(result)
    return result


def compact_custody(archive: dict[str, object], *, retain_tail: int) -> dict[str, object]:
    events = copy.deepcopy(archive.get("events", []))
    cut = max(0, len(events) - retain_tail)
    prefix = events[:cut]
    anchor_body = {
        "compacted_count": cut,
        "prefix_sha256": _digest(prefix),
        "prefix_head_sha256": prefix[-1]["event_sha256"] if prefix else "0" * 64,
    }
    anchor = {**anchor_body, "anchor_sha256": _digest(anchor_body)}
    result = {
        "entry": copy.deepcopy(archive.get("entry")),
        "anchor": anchor,
        "retained_events": events[cut:],
        "full_events_sha256": _digest(events),
        "safety": _safety(),
    }
    result["compaction_sha256"] = _digest(result)
    return result


def reconstruct_custody(
    compacted: dict[str, object], prefix: list[dict[str, object]], *, evaluated_at: str
) -> dict[str, object]:
    errors = []
    anchor = compacted.get("anchor", {})
    if _digest(prefix) != anchor.get("prefix_sha256") or len(prefix) != anchor.get(
        "compacted_count"
    ):
        errors.append("PREFIX_INVALID")
    events = copy.deepcopy(prefix) + copy.deepcopy(compacted.get("retained_events", []))
    if _digest(events) != compacted.get("full_events_sha256"):
        errors.append("FULL_HISTORY_INVALID")
    validation = validate_archive(
        {"schema": SCHEMA, "entry": compacted.get("entry"), "events": events},
        evaluated_at=evaluated_at,
    )
    errors.extend(validation["errors"])
    result = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "events": events,
        "audit_state": validation["state"],
        "safety": _safety(),
    }
    result["reconstruction_sha256"] = _digest(result)
    return result


def _result(errors: list[str], state: str, count: int) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "state": state,
        "event_count": count,
        "physical_deletion": False,
        "safety": _safety(),
    }


def _safety() -> dict[str, bool]:
    return {
        "simulation_only": True,
        "physical_deletion": False,
        "filesystem_write": False,
        "network_access": False,
        "runtime_write": False,
        "service_control": False,
        "order_capability": False,
    }
