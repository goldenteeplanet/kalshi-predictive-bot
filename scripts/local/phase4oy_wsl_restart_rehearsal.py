"""Offline WSL restart and service-invariant recovery rehearsal."""

from __future__ import annotations

import hashlib
import json

SCHEMA = "phase4oy.wsl-restart-rehearsal.v1"
TRANSITIONS = (
    "PROCESS_CRASH",
    "WSL_TERMINATED",
    "UBUNTU_RELAUNCHED",
    "SYSTEMD_READY",
    "INVARIANTS_VERIFIED",
    "CHECKPOINT_RESTORED",
    "BOT_ACTIVE",
    "UI_ACTIVE",
    "HEALTHY",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def invariant_state() -> dict[str, bool]:
    return {
        "paper_order_creation": False,
        "paper_kill_switch": True,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }


def record_transition(
    *,
    transition: str,
    sequence: int,
    bot_active: bool,
    ui_active: bool,
    checkpoint_fresh: bool,
    invariants: dict[str, bool] | None = None,
    restart_attempt: int = 1,
) -> dict[str, object]:
    body = {
        "schema": SCHEMA,
        "transition": transition,
        "sequence": sequence,
        "bot_active": bot_active,
        "ui_active": ui_active,
        "checkpoint_fresh": checkpoint_fresh,
        "invariants": invariants or invariant_state(),
        "restart_attempt": restart_attempt,
        "offline_rehearsal": True,
        "safety": _safety(),
    }
    return {**body, "transition_sha256": _digest(body)}


def build_passing_trace() -> list[dict[str, object]]:
    rows = []
    for sequence, transition in enumerate(TRANSITIONS, start=1):
        rows.append(
            record_transition(
                transition=transition,
                sequence=sequence,
                bot_active=transition in {"BOT_ACTIVE", "UI_ACTIVE", "HEALTHY"},
                ui_active=transition in {"UI_ACTIVE", "HEALTHY"},
                checkpoint_fresh=transition
                in {"CHECKPOINT_RESTORED", "BOT_ACTIVE", "UI_ACTIVE", "HEALTHY"},
            )
        )
    return rows


def certify_rehearsal(trace: list[dict[str, object]]) -> dict[str, object]:
    errors: list[str] = []
    names = [row.get("transition") for row in trace]
    if names != list(TRANSITIONS):
        errors.append("STARTUP_ORDER_INVALID")
    for index, row in enumerate(trace, start=1):
        transition = TRANSITIONS[index - 1] if index <= len(TRANSITIONS) else "UNKNOWN"
        unsigned = {key: value for key, value in row.items() if key != "transition_sha256"}
        if row.get("transition_sha256") != _digest(unsigned):
            errors.append(f"{transition}:TRANSITION_HASH_MISMATCH")
        if row.get("sequence") != index:
            errors.append(f"{transition}:SEQUENCE_INVALID")
        if row.get("invariants") != invariant_state():
            errors.append(f"{transition}:EXECUTION_INVARIANT_VIOLATION")
        if row.get("offline_rehearsal") is not True or row.get("safety") != _safety():
            errors.append(f"{transition}:REHEARSAL_SAFETY_INVALID")
        attempt = row.get("restart_attempt")
        if not isinstance(attempt, int) or attempt < 1 or attempt > 3:
            errors.append(f"{transition}:RESTART_LOOP_LIMIT_EXCEEDED")
        if index < 7 and row.get("bot_active") is not False:
            errors.append(f"{transition}:BOT_STARTED_BEFORE_VERIFICATION")
        if index < 8 and row.get("ui_active") is not False:
            errors.append(f"{transition}:UI_STARTED_BEFORE_BOT")
        if index >= 6 and row.get("checkpoint_fresh") is not True:
            errors.append(f"{transition}:STALE_CHECKPOINT")
        if transition == "HEALTHY" and (
            row.get("bot_active") is not True or row.get("ui_active") is not True
        ):
            errors.append("HEALTH_CLAIM_BEFORE_SERVICES_ACTIVE")
    healthy = not errors and len(trace) == len(TRANSITIONS)
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if healthy else "REFUSE",
        "errors": sorted(set(errors)),
        "transition_count": len(trace),
        "transition_order": list(TRANSITIONS),
        "bot_service": "kalshi-fixed-rate-refresh.service",
        "ui_service": "kalshi-ui.service",
        "final_health": "HEALTHY" if healthy else "UNHEALTHY_FROZEN",
        "paper_position_preserved": "KXRAINAUSM-26AUG-1 contract 1; no additional order authorized",
        "actual_restart_performed": False,
        "executable": False,
        "safety": _safety(),
    }
    return {**body, "certificate_sha256": _digest(body)}


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
