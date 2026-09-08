"""Offline alert acknowledgement, retry, and escalation state-machine proof."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4pa_alert_routing import build_alert

SCHEMA = "phase4pb.alert-delivery-state.v1"
MAX_ATTEMPTS = 3


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def new_state(alert: dict[str, object]) -> dict[str, object]:
    state = {
        "schema": SCHEMA,
        "alert_sha256": alert["alert_sha256"],
        "deduplication_key": alert["deduplication_key"],
        "status": "PENDING",
        "attempt": 0,
        "active_attempt_id": None,
        "acknowledged_attempt_id": None,
        "next_retry_tick": 0,
        "escalated": False,
        "refused": False,
        "errors": [],
        "processed_events": {},
        "last_event_sha256": "GENESIS",
        "delivery_mode": "DRY_RUN",
        "real_notifications_sent": 0,
        "safety": _safety(),
    }
    return _sign_state(state)


def _sign_state(state: dict[str, object]) -> dict[str, object]:
    unsigned = {key: value for key, value in state.items() if key != "state_sha256"}
    return {**unsigned, "state_sha256": _digest(unsigned)}


def event(event_id: str, kind: str, tick: int, **fields: object) -> dict[str, object]:
    body = {"event_id": event_id, "kind": kind, "tick": tick, **fields}
    return {**body, "event_sha256": _digest(body)}


def apply_event(state: dict[str, object], item: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(state)
    unsigned = {key: value for key, value in item.items() if key != "event_sha256"}
    event_hash = _digest(unsigned)
    errors: list[str] = []
    unsigned_state = {key: value for key, value in result.items() if key != "state_sha256"}
    if result.get("state_sha256") != _digest(unsigned_state):
        errors.append("STATE_HASH_MISMATCH")
    if item.get("event_sha256") != event_hash:
        errors.append("EVENT_HASH_MISMATCH")
    event_id = item.get("event_id")
    prior = result["processed_events"].get(event_id)
    if prior is not None:
        if prior == event_hash and not errors:
            return result
        errors.append("CONFLICTING_DUPLICATE_EVENT")
    if result["refused"]:
        errors.append("STATE_ALREADY_REFUSED")
    try:
        tick = int(item["tick"])
        kind = str(item["kind"])
    except (KeyError, TypeError, ValueError):
        errors.append("EVENT_MALFORMED")
        tick, kind = -1, "INVALID"
    if tick < 0:
        errors.append("TICK_INVALID")

    if not errors and kind == "ATTEMPT":
        if result["status"] not in {"PENDING", "RETRY_WAIT"}:
            errors.append("ATTEMPT_OUT_OF_ORDER")
        elif tick < result["next_retry_tick"]:
            errors.append("RETRY_TOO_EARLY")
        elif result["attempt"] >= MAX_ATTEMPTS:
            errors.append("ATTEMPT_LIMIT_EXCEEDED")
        else:
            result["attempt"] += 1
            result["active_attempt_id"] = f"{result['alert_sha256']}:{result['attempt']}"
            result["status"] = "IN_FLIGHT"
    elif not errors and kind == "TIMEOUT":
        if result["status"] != "IN_FLIGHT" or item.get("attempt_id") != result["active_attempt_id"]:
            errors.append("TIMEOUT_OUT_OF_ORDER")
        elif result["attempt"] >= MAX_ATTEMPTS:
            result["status"] = "ESCALATED"
            result["escalated"] = True
        else:
            result["status"] = "RETRY_WAIT"
            result["next_retry_tick"] = tick + (2 ** (result["attempt"] - 1))
    elif not errors and kind == "ACK":
        if result["status"] != "IN_FLIGHT" or item.get("attempt_id") != result["active_attempt_id"]:
            errors.append("ACK_OUT_OF_ORDER_OR_STALE")
        else:
            result["status"] = "ACKED"
            result["acknowledged_attempt_id"] = result["active_attempt_id"]
    elif not errors and kind == "ESCALATE":
        if result["status"] not in {"IN_FLIGHT", "RETRY_WAIT"}:
            errors.append("ESCALATION_OUT_OF_ORDER")
        else:
            result["status"] = "ESCALATED"
            result["escalated"] = True
    elif not errors and kind not in {"ATTEMPT", "TIMEOUT", "ACK", "ESCALATE"}:
        errors.append("EVENT_KIND_UNKNOWN")

    if errors:
        result["status"] = "REFUSED"
        result["refused"] = True
        result["errors"] = sorted(set([*result["errors"], *errors]))
    result["processed_events"][event_id] = event_hash
    result["last_event_sha256"] = _digest(
        {"previous": result["last_event_sha256"], "event": event_hash}
    )
    return _sign_state(result)


def replay(alert: dict[str, object], events: list[dict[str, object]]) -> dict[str, object]:
    state = new_state(alert)
    for item in events:
        state = apply_event(state, item)
    return state


def certify_delivery_state_machine() -> dict[str, object]:
    alert = build_alert(
        refusal_code="BOT_SERVICE_MISSING",
        stage="BOT_ACTIVE",
        occurrence=3,
        recovery_succeeded=False,
    )
    first = event("attempt-1", "ATTEMPT", 0)
    attempt_one = f"{alert['alert_sha256']}:1"
    lost_ack = [first, event("timeout-1", "TIMEOUT", 1, attempt_id=attempt_one)]
    restored = replay(alert, lost_ack)
    retry = event("attempt-2", "ATTEMPT", restored["next_retry_tick"])
    attempt_two = f"{alert['alert_sha256']}:2"
    success_events = [*lost_ack, retry, event("ack-2", "ACK", 3, attempt_id=attempt_two)]
    success = replay(alert, success_events)
    restarted = replay(alert, success_events)

    exhausted_events = [first, lost_ack[1]]
    tick = 2
    for attempt_number in (2, 3):
        exhausted_events.append(event(f"attempt-{attempt_number}", "ATTEMPT", tick))
        attempt_id = f"{alert['alert_sha256']}:{attempt_number}"
        exhausted_events.append(
            event(f"timeout-{attempt_number}", "TIMEOUT", tick + 1, attempt_id=attempt_id)
        )
        tick += 3
    exhausted = replay(alert, exhausted_events)
    body = {
        "schema": SCHEMA,
        "verdict": "PASS"
        if success["status"] == "ACKED"
        and restarted == success
        and exhausted["status"] == "ESCALATED"
        else "REFUSE",
        "lost_ack_retried": success["attempt"] == 2,
        "restart_replay_identical": restarted == success,
        "retry_exhaustion_escalated": exhausted["escalated"],
        "successful_state_sha256": _digest(success),
        "exhausted_state_sha256": _digest(exhausted),
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
