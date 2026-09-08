"""Replay and rollback a deterministic synthetic partial-fill state machine."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ep.state-machine-input.v1"
REPORT_SCHEMA = "phase4ep.state-machine-report.v1"
EVENT_TYPES = {"FILL", "EXPIRE", "CANCEL", "AMBIGUOUS", "ROLLBACK"}
TERMINAL_STATES = {"EXPIRED", "CANCELLED", "AMBIGUOUS"}


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _positive_int(value: Any, error: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(error)
    return value


def _state(filled: int, requested: int) -> str:
    if filled == 0:
        return "OPEN"
    if filled < requested:
        return "PARTIAL"
    return "COMPLETE"


def _replay(requested: int, events: list[dict[str, Any]]) -> tuple[str, int, list[dict[str, Any]]]:
    current = "OPEN"
    filled = 0
    active_fills: list[tuple[int, int]] = []
    transitions = []
    for event in events:
        before = current
        event_type = event["event_type"]
        if current in TERMINAL_STATES:
            raise ValueError("PHASE4EP_EVENT_AFTER_TERMINAL")
        if event_type == "FILL":
            if current == "COMPLETE":
                raise ValueError("PHASE4EP_FILL_AFTER_COMPLETE")
            if filled + event["quantity"] > requested:
                raise ValueError("PHASE4EP_OVERFILL")
            filled += event["quantity"]
            active_fills.append((event["sequence"], event["quantity"]))
            current = _state(filled, requested)
        elif event_type == "ROLLBACK":
            if not active_fills or event["target_sequence"] != active_fills[-1][0]:
                raise ValueError("PHASE4EP_ROLLBACK_TARGET_INVALID")
            _, quantity = active_fills.pop()
            filled -= quantity
            current = _state(filled, requested)
        elif current == "COMPLETE":
            raise ValueError("PHASE4EP_EVENT_AFTER_COMPLETE")
        elif event_type == "EXPIRE":
            current = "EXPIRED"
        elif event_type == "CANCEL":
            current = "CANCELLED"
        else:
            current = "AMBIGUOUS"
        transitions.append(
            {
                "sequence": event["sequence"],
                "event_type": event_type,
                "state_before": before,
                "state_after": current,
                "filled_quantity_after": filled,
                "remaining_quantity_after": requested - filled,
                "target_sequence": event["target_sequence"],
            }
        )
    return current, filled, transitions


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {
        "schema",
        "synthetic_order_id",
        "requested_quantity",
        "events",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EP_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EP_INPUT_SCHEMA_OR_HASH_INVALID")
    order_id = payload["synthetic_order_id"]
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("PHASE4EP_ORDER_ID_INVALID")
    requested = _positive_int(payload["requested_quantity"], "PHASE4EP_REQUESTED_QUANTITY_INVALID")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("PHASE4EP_EVENTS_INVALID")
    normalized = []
    seen_sequences = set()
    fields = {"sequence", "event_type", "quantity", "target_sequence"}
    for event in events:
        if not isinstance(event, dict) or set(event) != fields:
            raise ValueError("PHASE4EP_EVENT_FIELDS_INVALID")
        sequence = _positive_int(event["sequence"], "PHASE4EP_SEQUENCE_INVALID")
        if sequence in seen_sequences:
            raise ValueError("PHASE4EP_SEQUENCE_DUPLICATE")
        seen_sequences.add(sequence)
        event_type = event["event_type"]
        if event_type not in EVENT_TYPES:
            raise ValueError("PHASE4EP_EVENT_TYPE_INVALID")
        quantity = _positive_int(
            event["quantity"], "PHASE4EP_EVENT_QUANTITY_INVALID", allow_zero=True
        )
        target = event["target_sequence"]
        if event_type == "FILL":
            if quantity <= 0 or target is not None:
                raise ValueError("PHASE4EP_FILL_FIELDS_INVALID")
        elif event_type == "ROLLBACK":
            if (
                quantity != 0
                or isinstance(target, bool)
                or not isinstance(target, int)
                or target <= 0
            ):
                raise ValueError("PHASE4EP_ROLLBACK_FIELDS_INVALID")
        elif quantity != 0 or target is not None:
            raise ValueError("PHASE4EP_TERMINAL_FIELDS_INVALID")
        normalized.append(event)
    normalized.sort(key=lambda row: row["sequence"])
    if [row["sequence"] for row in normalized] != list(range(1, len(normalized) + 1)):
        raise ValueError("PHASE4EP_SEQUENCE_GAP")
    final_state, filled, transitions = _replay(requested, normalized)
    replay_state, replay_filled, replay_transitions = _replay(requested, normalized)
    if (final_state, filled, transitions) != (replay_state, replay_filled, replay_transitions):
        raise ValueError("PHASE4EP_REPLAY_MISMATCH")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EP",
        "input_hash": payload["artifact_hash"],
        "synthetic_order_id": order_id,
        "requested_quantity": requested,
        "events": normalized,
        "transitions": transitions,
        "final_state": final_state,
        "filled_quantity": filled,
        "remaining_quantity": requested - filled,
        "replay_exact": True,
        "real_paper_fills_modified": 0,
        "real_paper_fills_created": 0,
        "database_writes": 0,
        "execution_authorized": False,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
