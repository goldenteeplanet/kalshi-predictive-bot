"""Offline cutover ceremony simulator with operator error injection."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4nx_migration_authorization import consume_simulation_grant

SCHEMA = "phase4ny.cutover-ceremony.v1"
STEPS = (
    "OBSERVE_WITNESS_STATE",
    "READ_BACK_ACTION",
    "ACKNOWLEDGE_CHECKLIST",
    "VERIFY_CHECKPOINT",
    "CONFIRM_QUORUM",
    "CONFIRM_ROLLBACK",
    "EXECUTE_SIMULATION",
    "POST_STAGE_VERIFY",
)
ROLE_FOR_STEP = {
    "OBSERVE_WITNESS_STATE": "observer",
    "READ_BACK_ACTION": "executor",
    "ACKNOWLEDGE_CHECKLIST": "observer",
    "VERIFY_CHECKPOINT": "verifier",
    "CONFIRM_QUORUM": "verifier",
    "CONFIRM_ROLLBACK": "observer",
    "EXECUTE_SIMULATION": "executor",
    "POST_STAGE_VERIFY": "verifier",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def build_ceremony(
    plan: dict[str, object],
    grant: dict[str, object],
    *,
    roles: dict[str, str],
    checkpoint_anchor: str,
    quorum_sha256: str,
    rollback_sha256: str,
    start_tick: int = 0,
) -> dict[str, object]:
    events = []
    tick = start_tick
    for stage in range(grant["stage_start"], grant["stage_end"] + 1):
        action = plan["actions"][stage]
        target = action.get("witness_id") or action.get("revoke_witness")
        for step in STEPS:
            event = {
                "stage": stage,
                "step": step,
                "actor": roles[ROLE_FOR_STEP[step]],
                "tick": tick,
                "plan_sha256": plan["plan_sha256"],
                "action_sha256": _digest(action),
                "target_witness": target,
                "response": "CONFIRMED",
                "witness_state_sha256": _digest({"stage": stage, "target": target}),
                "checkpoint_anchor": checkpoint_anchor,
                "quorum_sha256": quorum_sha256,
                "rollback_sha256": rollback_sha256,
                "threshold": action.get("threshold"),
                "post_verify_passed": True,
                "premature_revocation": False,
                "interrupted": False,
            }
            event["event_sha256"] = _digest(event)
            events.append(event)
            tick += 1
    body = {
        "schema": SCHEMA,
        "plan_sha256": plan["plan_sha256"],
        "authorization_id": grant["authorization_id"],
        "stage_start": grant["stage_start"],
        "stage_end": grant["stage_end"],
        "roles": roles,
        "checkpoint_anchor": checkpoint_anchor,
        "quorum_sha256": quorum_sha256,
        "rollback_sha256": rollback_sha256,
        "events": events,
    }
    return {**body, "ceremony_sha256": _digest(body)}


def simulate_ceremony(
    ceremony: dict[str, object],
    plan: dict[str, object],
    grant: dict[str, object],
    *,
    used_authorization_ids: set[str],
    maximum_observation_age: int,
    maximum_step_gap: int,
) -> dict[str, object]:
    errors = []
    terminal = "COMPLETED"
    roles = ceremony.get("roles", {})
    if len({roles.get("observer"), roles.get("executor"), roles.get("verifier")}) != 3:
        errors.append("OPERATOR_ROLE_SEPARATION_FAILED")
    if ceremony.get("plan_sha256") != plan.get("plan_sha256"):
        errors.append("PLAN_HASH_MISMATCH")
    consumption = consume_simulation_grant(
        grant, stage=grant["stage_start"], used_authorization_ids=used_authorization_ids
    )
    if consumption["verdict"] != "PASS":
        errors.extend(consumption["errors"])
    expected = [
        (stage, step)
        for stage in range(grant["stage_start"], grant["stage_end"] + 1)
        for step in STEPS
    ]
    events = ceremony.get("events", [])
    transcript = []
    failed_index = None
    last_tick = None
    observation_tick = {}
    for index, expected_item in enumerate(expected):
        if index >= len(events):
            errors.append("CEREMONY_STEP_SKIPPED")
            failed_index = index
            terminal = "PAUSED"
            break
        event = events[index]
        stage, step = expected_item
        event_errors = []
        if (event.get("stage"), event.get("step")) != expected_item:
            event_errors.append("CEREMONY_STEP_OUT_OF_ORDER")
        action = plan["actions"][stage]
        target = action.get("witness_id") or action.get("revoke_witness")
        if event.get("actor") != roles.get(ROLE_FOR_STEP[step]):
            event_errors.append("OPERATOR_ROLE_MISMATCH")
        if event.get("plan_sha256") != plan["plan_sha256"]:
            event_errors.append("PLAN_HASH_MISMATCH")
        if event.get("action_sha256") != _digest(action):
            event_errors.append("READBACK_MISMATCH")
        if event.get("target_witness") != target:
            event_errors.append("WRONG_WITNESS_SELECTION")
        if event.get("response") != "CONFIRMED":
            event_errors.append("AMBIGUOUS_OPERATOR_RESPONSE")
        tick = event.get("tick")
        if not isinstance(tick, int) or (
            last_tick is not None and tick - last_tick > maximum_step_gap
        ):
            event_errors.append("CEREMONY_TIMEOUT")
        last_tick = tick if isinstance(tick, int) else last_tick
        if step == "OBSERVE_WITNESS_STATE":
            observation_tick[stage] = tick
        elif isinstance(tick, int) and (
            stage not in observation_tick
            or tick - observation_tick[stage] > maximum_observation_age
        ):
            event_errors.append("STALE_WITNESS_OBSERVATION")
        if event.get("checkpoint_anchor") != ceremony.get("checkpoint_anchor"):
            event_errors.append("CHECKPOINT_VERIFICATION_FAILED")
        if event.get("quorum_sha256") != ceremony.get("quorum_sha256"):
            event_errors.append("QUORUM_CONFIRMATION_FAILED")
        if event.get("rollback_sha256") != ceremony.get("rollback_sha256"):
            event_errors.append("ROLLBACK_NOT_READY")
        if step == "EXECUTE_SIMULATION" and event.get("threshold") != action.get("threshold"):
            event_errors.append("THRESHOLD_MISTAKE")
        if event.get("premature_revocation"):
            event_errors.append("PREMATURE_REVOCATION")
        if event.get("interrupted"):
            event_errors.append("CEREMONY_INTERRUPTED")
        if step == "POST_STAGE_VERIFY" and event.get("post_verify_passed") is not True:
            event_errors.append("POST_STAGE_VERIFICATION_FAILED")
        previous = transcript[-1]["transcript_sha256"] if transcript else None
        row = {
            "index": index,
            "stage": event.get("stage"),
            "step": event.get("step"),
            "actor": event.get("actor"),
            "errors": sorted(set(event_errors)),
            "previous_transcript_sha256": previous,
        }
        row["transcript_sha256"] = _digest(row)
        transcript.append(row)
        if event_errors:
            errors.extend(event_errors)
            failed_index = index
            terminal = (
                "ROLLED_BACK"
                if "ROLLBACK_NOT_READY" not in event_errors and ceremony.get("rollback_sha256")
                else "PAUSED"
            )
            break
    if failed_index is not None and len(events) > failed_index + 1:
        errors.append("CONTINUATION_AFTER_ROLLBACK")
    if failed_index is None and len(events) != len(expected):
        errors.append("UNEXPECTED_EXTRA_CEREMONY_STEP")
        terminal = "PAUSED"
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "terminal_state": terminal,
        "completed_stages": len(
            {
                row["stage"]
                for row in transcript
                if row["step"] == "POST_STAGE_VERIFY" and not row["errors"]
            }
        ),
        "transcript": transcript,
        "authorization_id": grant.get("authorization_id"),
        "authorization_consumed": consumption["verdict"] == "PASS",
        "safety": _safety(),
    }
    result["transcript_root_sha256"] = _digest(result)
    return result


def inject_operator_fault(ceremony: dict[str, object], fault: str) -> dict[str, object]:
    value = copy.deepcopy(ceremony)
    events = value["events"]
    if fault == "SKIPPED_STEP":
        events.pop(1)
    elif fault == "OUT_OF_ORDER":
        events[1], events[2] = events[2], events[1]
    elif fault == "WRONG_WITNESS":
        events[0]["target_witness"] = "wrong-witness"
    elif fault == "STALE_OBSERVATION":
        events[1]["tick"] = events[0]["tick"] + 100
    elif fault == "INCORRECT_READBACK":
        events[1]["action_sha256"] = "0" * 64
    elif fault == "MISMATCHED_PLAN":
        events[0]["plan_sha256"] = "0" * 64
    elif fault == "PREMATURE_REVOCATION":
        events[0]["premature_revocation"] = True
    elif fault == "THRESHOLD_MISTAKE":
        execute = next(row for row in events if row["step"] == "EXECUTE_SIMULATION")
        execute["threshold"] = 999
    elif fault == "AMBIGUOUS_RESPONSE":
        events[2]["response"] = "maybe"
    elif fault == "FAILED_VERIFICATION":
        post = next(row for row in events if row["step"] == "POST_STAGE_VERIFY")
        post["post_verify_passed"] = False
    elif fault == "TIMEOUT":
        events[1]["tick"] = events[0]["tick"] + 100
    elif fault == "INTERRUPTION":
        events[3]["interrupted"] = True
    elif fault == "CONTINUE_AFTER_ROLLBACK":
        events[7]["post_verify_passed"] = False
    else:
        raise ValueError("unknown operator fault")
    return value


def _safety():
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
