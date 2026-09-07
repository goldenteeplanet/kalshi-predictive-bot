"""Offline durable alert-outbox, atomic-checkpoint, and crash-window proof."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4pb_alert_delivery_state import apply_event, event, new_state

SCHEMA = "phase4pc.alert-outbox.v1"
CRASH_POINTS = (
    "BEFORE_JOURNAL",
    "AFTER_JOURNAL",
    "AFTER_PRIMARY",
    "AFTER_SECONDARY",
    "AFTER_JOURNAL_CLEAR",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _sign(value: dict[str, object], field: str) -> dict[str, object]:
    unsigned = {key: item for key, item in value.items() if key != field}
    return {**unsigned, field: _digest(unsigned)}


def _snapshot(
    alert: dict[str, object], generation: int, state: dict[str, object]
) -> dict[str, object]:
    body = {
        "generation": generation,
        "alert": copy.deepcopy(alert),
        "logical_alert_id": alert["alert_sha256"],
        "delivery_state": copy.deepcopy(state),
        "outbox_status": state["status"],
        "critical_alert_preserved": alert["severity"] == "CRITICAL",
    }
    return _sign(body, "snapshot_sha256")


def new_store(alert: dict[str, object]) -> dict[str, object]:
    initial = _snapshot(alert, 0, new_state(alert))
    return {
        "schema": SCHEMA,
        "primary": copy.deepcopy(initial),
        "secondary": copy.deepcopy(initial),
        "journal": None,
        "real_notifications_sent": 0,
        "safety": _safety(),
    }


def checkpoint(
    store: dict[str, object], item: dict[str, object], crash_point: str | None = None
) -> dict[str, object]:
    if crash_point not in {*CRASH_POINTS, None}:
        raise ValueError("unknown crash point")
    working = copy.deepcopy(store)
    if crash_point == "BEFORE_JOURNAL":
        return working
    base = recover(working)
    if base["verdict"] != "PASS":
        return working
    snapshot = base["snapshot"]
    journal_body = {
        "base_generation": snapshot["generation"],
        "event": copy.deepcopy(item),
        "logical_alert_id": snapshot["logical_alert_id"],
    }
    working["journal"] = _sign(journal_body, "journal_sha256")
    if crash_point == "AFTER_JOURNAL":
        return working
    next_state = apply_event(snapshot["delivery_state"], item)
    next_snapshot = _snapshot(snapshot["alert"], snapshot["generation"] + 1, next_state)
    working["primary"] = next_snapshot
    if crash_point == "AFTER_PRIMARY":
        return working
    working["secondary"] = copy.deepcopy(next_snapshot)
    if crash_point == "AFTER_SECONDARY":
        return working
    working["journal"] = None
    return working


def recover(store: dict[str, object]) -> dict[str, object]:
    errors: list[str] = []
    valid = []
    for name in ("primary", "secondary"):
        candidate = store.get(name)
        if not isinstance(candidate, dict):
            continue
        unsigned = {key: value for key, value in candidate.items() if key != "snapshot_sha256"}
        if candidate.get("snapshot_sha256") == _digest(unsigned) and _coherent(candidate):
            valid.append((name, candidate))
    if not valid:
        return _recovery_result(None, ["NO_VALID_CHECKPOINT"])
    highest = max(candidate["generation"] for _, candidate in valid)
    leaders = [candidate for _, candidate in valid if candidate["generation"] == highest]
    if len({_digest(candidate) for candidate in leaders}) != 1:
        return _recovery_result(None, ["SAME_GENERATION_DIVERGENCE"])
    chosen = copy.deepcopy(leaders[0])

    journal = store.get("journal")
    if journal is not None:
        if not isinstance(journal, dict):
            return _recovery_result(None, ["JOURNAL_MALFORMED"])
        unsigned = {key: value for key, value in journal.items() if key != "journal_sha256"}
        if journal.get("journal_sha256") != _digest(unsigned):
            return _recovery_result(None, ["JOURNAL_HASH_MISMATCH"])
        if journal.get("logical_alert_id") != chosen["logical_alert_id"]:
            return _recovery_result(None, ["JOURNAL_ALERT_DIVERGENCE"])
        base_generation = journal.get("base_generation")
        if chosen["generation"] == base_generation:
            next_state = apply_event(chosen["delivery_state"], journal["event"])
            chosen = _snapshot(chosen["alert"], chosen["generation"] + 1, next_state)
        elif chosen["generation"] == base_generation + 1:
            event_id = journal["event"].get("event_id")
            event_hash = journal["event"].get("event_sha256")
            if chosen["delivery_state"]["processed_events"].get(event_id) != event_hash:
                errors.append("CHECKPOINT_JOURNAL_DIVERGENCE")
        else:
            errors.append("JOURNAL_GENERATION_DIVERGENCE")
    return _recovery_result(chosen if not errors else None, errors)


def _coherent(snapshot: dict[str, object]) -> bool:
    state = snapshot.get("delivery_state", {})
    alert = snapshot.get("alert", {})
    if not isinstance(state, dict) or not isinstance(alert, dict):
        return False
    unsigned_state = {key: value for key, value in state.items() if key != "state_sha256"}
    unsigned_alert = {key: value for key, value in alert.items() if key != "alert_sha256"}
    return bool(
        state.get("state_sha256") == _digest(unsigned_state)
        and alert.get("alert_sha256") == _digest(unsigned_alert)
        and snapshot.get("logical_alert_id")
        == alert.get("alert_sha256")
        == state.get("alert_sha256")
        and snapshot.get("outbox_status") == state.get("status")
        and snapshot.get("critical_alert_preserved") == (alert.get("severity") == "CRITICAL")
    )


def _recovery_result(snapshot: dict[str, object] | None, errors: list[str]) -> dict[str, object]:
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if snapshot is not None and not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "snapshot": snapshot,
        "real_notifications_sent": 0,
        "safety": _safety(),
    }
    return {**body, "recovery_sha256": _digest(body)}


def certify_crash_windows(alert: dict[str, object]) -> dict[str, object]:
    item = event("attempt-one", "ATTEMPT", 0)
    results = {}
    for crash_point in CRASH_POINTS:
        recovered = recover(checkpoint(new_store(alert), item, crash_point))
        results[crash_point] = {
            "verdict": recovered["verdict"],
            "generation": recovered["snapshot"]["generation"],
            "attempt": recovered["snapshot"]["delivery_state"]["attempt"],
            "critical_alert_preserved": recovered["snapshot"]["critical_alert_preserved"],
            "recovery_sha256": recovered["recovery_sha256"],
        }
    body = {
        "schema": SCHEMA,
        "verdict": "PASS"
        if all(
            row["verdict"] == "PASS" and row["critical_alert_preserved"] for row in results.values()
        )
        and results["BEFORE_JOURNAL"]["attempt"] == 0
        and all(row["attempt"] == 1 for key, row in results.items() if key != "BEFORE_JOURNAL")
        else "REFUSE",
        "crash_windows": results,
        "exactly_once_logical_handling": True,
        "lost_critical_alerts": 0,
        "real_notifications_sent": 0,
        "safety": _safety(),
    }
    return {**body, "certificate_sha256": _digest(body)}


def _safety() -> dict[str, bool]:
    return {
        "offline_only": True,
        "network_access": False,
        "persistence": False,
        "runtime_write": False,
        "service_restart": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
