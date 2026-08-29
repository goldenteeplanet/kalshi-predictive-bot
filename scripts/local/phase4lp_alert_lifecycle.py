"""Validate a deterministic alert-envelope lifecycle state machine."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime

SCHEMA = "phase4lp.alert-lifecycle.v1"
ENVELOPE_SCHEMA = "phase4lo.alert-delivery-envelope.v1"
STATES = {"CREATED", "VALIDATED", "PRESENTED", "ACKNOWLEDGED", "EXPIRED", "REJECTED", "ARCHIVED"}
TERMINAL_STATES = {"ACKNOWLEDGED", "EXPIRED", "REJECTED", "ARCHIVED"}
TRANSITIONS = {
    "CREATED": {"VALIDATED", "REJECTED"},
    "VALIDATED": {"PRESENTED", "EXPIRED", "REJECTED", "ARCHIVED"},
    "PRESENTED": {"ACKNOWLEDGED", "EXPIRED", "REJECTED", "ARCHIVED"},
}
EVENT_FIELDS = {
    "transition_id",
    "envelope_sha256",
    "from_state",
    "to_state",
    "occurred_at",
    "reason",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def validate_lifecycle(envelope: object, events: object) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(envelope, dict):
        errors.append("ENVELOPE_NOT_AN_OBJECT")
        envelope = {}
    elif envelope.get("schema") != ENVELOPE_SCHEMA:
        errors.append("ENVELOPE_BAD_SCHEMA")
    envelope_hash = envelope.get("envelope_sha256")
    envelope_body = {key: value for key, value in envelope.items() if key != "envelope_sha256"}
    if envelope_hash != _digest(envelope_body):
        errors.append("ENVELOPE_HASH_MISMATCH")
    if not isinstance(events, list):
        errors.append("EVENTS_NOT_A_LIST")
        events = []

    accepted: list[dict[str, object]] = []
    identities: dict[str, str] = {}
    last_time: datetime | None = None
    state = "CREATED"
    chain = "0" * 64
    for index, event in enumerate(events):
        prefix = f"EVENT_{index}"
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            errors.append(f"{prefix}:FIELD_SET_INVALID")
            continue
        event_id = event.get("transition_id")
        fingerprint = _digest(event)
        if not isinstance(event_id, str) or not event_id:
            errors.append(f"{prefix}:ID_INVALID")
            continue
        if event_id in identities:
            if identities[event_id] != fingerprint:
                errors.append(f"{prefix}:CONFLICTING_REPLAY")
            continue
        identities[event_id] = fingerprint
        occurred = _time(event.get("occurred_at"))
        if occurred is None:
            errors.append(f"{prefix}:TIME_INVALID")
            continue
        if last_time is not None and occurred < last_time:
            errors.append(f"{prefix}:TIME_REVERSED")
            continue
        if event.get("envelope_sha256") != envelope_hash:
            errors.append(f"{prefix}:ENVELOPE_BINDING_MISMATCH")
            continue
        from_state = event.get("from_state")
        to_state = event.get("to_state")
        if from_state not in STATES or to_state not in STATES:
            errors.append(f"{prefix}:STATE_INVALID")
            continue
        if state in TERMINAL_STATES:
            errors.append(f"{prefix}:POST_TERMINAL_TRANSITION")
            continue
        if from_state != state:
            errors.append(f"{prefix}:FROM_STATE_MISMATCH")
            continue
        if to_state not in TRANSITIONS.get(state, set()):
            errors.append(f"{prefix}:TRANSITION_INVALID")
            continue
        if not isinstance(event.get("reason"), str) or not 0 < len(event["reason"]) <= 200:
            errors.append(f"{prefix}:REASON_INVALID")
            continue
        record: dict[str, object] = {
            "sequence": len(accepted) + 1,
            **event,
            "previous_record_sha256": chain,
        }
        chain = _digest(record)
        record["record_sha256"] = chain
        accepted.append(record)
        state = str(to_state)
        last_time = occurred

    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "envelope_sha256": envelope_hash,
        "current_state": state if not errors else None,
        "terminal": state in TERMINAL_STATES if not errors else None,
        "records": accepted if not errors else [],
        "replay": {
            "input_count": len(events),
            "unique_transition_count": len(identities),
            "applied_transition_count": len(accepted),
            "chain_head_sha256": chain,
        },
        "safety": {
            "validation_only": True,
            "delivery_enabled": False,
            "state_write": False,
            "network_access": False,
            "service_control": False,
            "wsl_control": False,
            "order_capability": False,
        },
    }
    result["lifecycle_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("envelope")
    parser.add_argument("events")
    args = parser.parse_args()
    with open(args.envelope, encoding="utf-8") as stream:
        envelope = json.load(stream)
    with open(args.events, encoding="utf-8") as stream:
        events = json.load(stream)
    result = validate_lifecycle(envelope, events)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
