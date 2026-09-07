from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ep_partial_fill_state_machine.py"
    spec = importlib.util.spec_from_file_location("phase4ep_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(sequence, event_type, quantity=0, target=None):
    return {
        "sequence": sequence,
        "event_type": event_type,
        "quantity": quantity,
        "target_sequence": target,
    }


def _payload(module, events=None):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "synthetic_order_id": "synthetic-1",
        "requested_quantity": 5,
        "events": [] if events is None else events,
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_empty_event_stream_is_open_and_exactly_replayable():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["final_state"] == "OPEN"
    assert report["filled_quantity"] == 0
    assert report["replay_exact"] is True


def test_partial_then_complete_fill_conserves_quantity():
    module = _module()
    report = module.build_report(_payload(module, [_event(1, "FILL", 2), _event(2, "FILL", 3)]))
    assert [row["state_after"] for row in report["transitions"]] == ["PARTIAL", "COMPLETE"]
    assert report["filled_quantity"] == 5
    assert report["remaining_quantity"] == 0


@pytest.mark.parametrize(
    "event_type,state", [("EXPIRE", "EXPIRED"), ("CANCEL", "CANCELLED"), ("AMBIGUOUS", "AMBIGUOUS")]
)
def test_terminal_states_after_partial_fill(event_type, state):
    module = _module()
    report = module.build_report(_payload(module, [_event(1, "FILL", 2), _event(2, event_type)]))
    assert report["final_state"] == state
    assert report["filled_quantity"] == 2
    assert report["remaining_quantity"] == 3


def test_rollback_last_fill_reopens_complete_state_and_replay_matches():
    module = _module()
    events = [_event(1, "FILL", 2), _event(2, "FILL", 3), _event(3, "ROLLBACK", target=2)]
    report = module.build_report(_payload(module, events))
    assert report["final_state"] == "PARTIAL"
    assert report["filled_quantity"] == 2
    assert report["transitions"][-1]["state_before"] == "COMPLETE"
    assert report["transitions"][-1]["state_after"] == "PARTIAL"
    assert report["replay_exact"] is True


def test_nested_rollbacks_return_to_open():
    module = _module()
    events = [
        _event(1, "FILL", 2),
        _event(2, "FILL", 1),
        _event(3, "ROLLBACK", target=2),
        _event(4, "ROLLBACK", target=1),
    ]
    report = module.build_report(_payload(module, events))
    assert report["final_state"] == "OPEN"
    assert report["filled_quantity"] == 0


@pytest.mark.parametrize(
    "kind",
    ["overfill", "fill_after_complete", "after_terminal", "rollback_wrong", "rollback_empty"],
)
def test_invalid_transitions_fail_closed(kind):
    module = _module()
    if kind == "overfill":
        events = [_event(1, "FILL", 6)]
    elif kind == "fill_after_complete":
        events = [_event(1, "FILL", 5), _event(2, "FILL", 1)]
    elif kind == "after_terminal":
        events = [_event(1, "CANCEL"), _event(2, "FILL", 1)]
    elif kind == "rollback_wrong":
        events = [_event(1, "FILL", 1), _event(2, "FILL", 1), _event(3, "ROLLBACK", target=1)]
    else:
        events = [_event(1, "ROLLBACK", target=1)]
    with pytest.raises(ValueError):
        module.build_report(_payload(module, events))


@pytest.mark.parametrize(
    "kind",
    [
        "order",
        "requested",
        "duplicate_sequence",
        "gap",
        "type",
        "fill_quantity",
        "fill_target",
        "rollback_quantity",
        "rollback_target",
        "terminal_quantity",
        "fields",
    ],
)
def test_malformed_event_stream_fails_closed(kind):
    module = _module()
    payload = _payload(module, [_event(1, "FILL", 1)])
    if kind == "order":
        payload["synthetic_order_id"] = ""
    elif kind == "requested":
        payload["requested_quantity"] = True
    elif kind == "duplicate_sequence":
        payload["events"].append(_event(1, "CANCEL"))
    elif kind == "gap":
        payload["events"][0]["sequence"] = 2
    elif kind == "type":
        payload["events"][0]["event_type"] = "UNKNOWN"
    elif kind == "fill_quantity":
        payload["events"][0]["quantity"] = 0
    elif kind == "fill_target":
        payload["events"][0]["target_sequence"] = 1
    elif kind == "rollback_quantity":
        payload["events"] = [_event(1, "ROLLBACK", quantity=1, target=1)]
    elif kind == "rollback_target":
        payload["events"] = [_event(1, "ROLLBACK", target=True)]
    elif kind == "terminal_quantity":
        payload["events"] = [_event(1, "CANCEL", quantity=1)]
    else:
        payload["events"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_event_input_order_is_canonicalized_and_not_mutated():
    module = _module()
    payload = _payload(module, [_event(2, "FILL", 3), _event(1, "FILL", 2)])
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    report = module.build_report(payload)
    assert [event["sequence"] for event in report["events"]] == [1, 2]
    assert report["final_state"] == "COMPLETE"
    assert payload == original


def test_tampering_determinism_atomic_publication_and_no_real_fills(tmp_path: Path):
    module = _module()
    payload = _payload(module, [_event(1, "FILL", 2)])
    payload["requested_quantity"] = 6
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module, [_event(1, "FILL", 2)]))
    assert report == module.build_report(_payload(module, [_event(1, "FILL", 2)]))
    assert report["real_paper_fills_modified"] == 0
    assert report["database_writes"] == 0
    output = tmp_path / "state.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_fill_mutation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ep_partial_fill_state_machine.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_fill",
        "insert_fill",
        "/home/james",
    ):
        assert token not in source
