from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts.local.phase4lp_alert_lifecycle import validate_lifecycle


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _envelope() -> dict[str, object]:
    value: dict[str, object] = {"schema": "phase4lo.alert-delivery-envelope.v1", "payload": {}}
    value["envelope_sha256"] = _digest(value)
    return value


def _event(envelope, event_id, source, target, minute):
    return {
        "transition_id": event_id,
        "envelope_sha256": envelope["envelope_sha256"],
        "from_state": source,
        "to_state": target,
        "occurred_at": f"2026-08-28T20:{minute:02d}:00Z",
        "reason": "validated test transition",
    }


def _happy_path():
    envelope = _envelope()
    events = [
        _event(envelope, "one", "CREATED", "VALIDATED", 0),
        _event(envelope, "two", "VALIDATED", "PRESENTED", 1),
        _event(envelope, "three", "PRESENTED", "ACKNOWLEDGED", 2),
    ]
    return envelope, events


def test_happy_path_is_deterministic_hash_chained_and_terminal() -> None:
    envelope, events = _happy_path()
    first = validate_lifecycle(envelope, events)
    assert first == validate_lifecycle(envelope, events)
    assert first["verdict"] == "PASS"
    assert first["current_state"] == "ACKNOWLEDGED"
    assert first["terminal"] is True
    assert first["records"][1]["previous_record_sha256"] == first["records"][0]["record_sha256"]


def test_exact_replay_is_idempotent() -> None:
    envelope, events = _happy_path()
    events.insert(1, copy.deepcopy(events[0]))
    result = validate_lifecycle(envelope, events)
    assert result["verdict"] == "PASS"
    assert result["replay"]["input_count"] == 4
    assert result["replay"]["applied_transition_count"] == 3


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda events: events[1].update(from_state="CREATED"), "FROM_STATE_MISMATCH"),
        (lambda events: events[0].update(to_state="PRESENTED"), "TRANSITION_INVALID"),
        (lambda events: events[1].update(occurred_at="2026-08-28T19:00:00Z"), "TIME_REVERSED"),
        (lambda events: events[1].update(envelope_sha256="x" * 64), "ENVELOPE_BINDING_MISMATCH"),
    ],
)
def test_skipped_reversed_and_unbound_transitions_refuse(mutation, error: str) -> None:
    envelope, events = _happy_path()
    mutation(events)
    result = validate_lifecycle(envelope, events)
    assert result["verdict"] == "REFUSE"
    assert any(error in item for item in result["errors"])


def test_conflicting_replay_refuses() -> None:
    envelope, events = _happy_path()
    conflict = copy.deepcopy(events[0])
    conflict["reason"] = "different"
    events.insert(1, conflict)
    assert "EVENT_1:CONFLICTING_REPLAY" in validate_lifecycle(envelope, events)["errors"]


@pytest.mark.parametrize("terminal", ["ACKNOWLEDGED", "EXPIRED", "REJECTED", "ARCHIVED"])
def test_all_terminal_states_are_immutable(terminal: str) -> None:
    envelope = _envelope()
    if terminal == "ACKNOWLEDGED":
        events = [
            _event(envelope, "one", "CREATED", "VALIDATED", 0),
            _event(envelope, "two", "VALIDATED", "PRESENTED", 1),
            _event(envelope, "three", "PRESENTED", terminal, 2),
        ]
    elif terminal == "REJECTED":
        events = [_event(envelope, "one", "CREATED", terminal, 0)]
    else:
        events = [
            _event(envelope, "one", "CREATED", "VALIDATED", 0),
            _event(envelope, "two", "VALIDATED", terminal, 1),
        ]
    events.append(_event(envelope, "after", terminal, "ARCHIVED", 3))
    result = validate_lifecycle(envelope, events)
    assert any("POST_TERMINAL_TRANSITION" in item for item in result["errors"])


def test_envelope_tampering_and_event_field_extension_refuse() -> None:
    envelope, events = _happy_path()
    envelope["payload"] = {"changed": True}
    assert "ENVELOPE_HASH_MISMATCH" in validate_lifecycle(envelope, events)["errors"]
    envelope, events = _happy_path()
    events[0]["delivery"] = True
    assert "EVENT_0:FIELD_SET_INVALID" in validate_lifecycle(envelope, events)["errors"]


def test_empty_lifecycle_remains_created_and_nonterminal() -> None:
    result = validate_lifecycle(_envelope(), [])
    assert result["verdict"] == "PASS"
    assert (result["current_state"], result["terminal"]) == ("CREATED", False)


def test_model_has_no_action_capability() -> None:
    safety = validate_lifecycle(_envelope(), [])["safety"]
    assert safety["validation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "validation_only")
