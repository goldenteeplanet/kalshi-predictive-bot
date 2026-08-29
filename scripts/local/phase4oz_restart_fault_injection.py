"""Restart-rehearsal fault injection and invariant observability proof."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4oy_wsl_restart_rehearsal import build_passing_trace, certify_rehearsal

SCHEMA = "phase4oz.restart-fault-injection.v1"
FAULTS = {
    "TRANSITION_OMISSION": tuple(range(9)),
    "TRANSITION_DELAY": tuple(range(9)),
    "TRANSITION_DUPLICATION": tuple(range(9)),
    "SERVICE_CRASH": (6, 7, 8),
    "CHECKPOINT_STALENESS": (5, 6, 7, 8),
    "INVARIANT_DRIFT": tuple(range(9)),
    "KILL_SWITCH_LOSS": tuple(range(9)),
    "FALSE_HEALTH": tuple(range(8)),
    "RESTART_STORM": tuple(range(2, 9)),
    "TELEMETRY_LOSS": tuple(range(9)),
}
EXPECTED_CODES = {
    "TRANSITION_OMISSION": "STARTUP_ORDER_INVALID",
    "TRANSITION_DELAY": "SEQUENCE_INVALID",
    "TRANSITION_DUPLICATION": "STARTUP_ORDER_INVALID",
    "SERVICE_CRASH": "BOT_SERVICE_MISSING",
    "CHECKPOINT_STALENESS": "STALE_CHECKPOINT",
    "INVARIANT_DRIFT": "EXECUTION_INVARIANT_VIOLATION",
    "KILL_SWITCH_LOSS": "EXECUTION_INVARIANT_VIOLATION",
    "FALSE_HEALTH": "STARTUP_ORDER_INVALID",
    "RESTART_STORM": "RESTART_LOOP_LIMIT_EXCEEDED",
    "TELEMETRY_LOSS": "TRANSITION_HASH_MISMATCH",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def inject_fault(trace: list[dict[str, object]], fault: str, stage_index: int):
    if fault not in FAULTS or stage_index not in FAULTS[fault]:
        raise ValueError("fault is not applicable at stage")
    mutated = copy.deepcopy(trace)
    if fault == "TRANSITION_OMISSION":
        mutated.pop(stage_index)
    elif fault == "TRANSITION_DELAY":
        mutated[stage_index]["sequence"] += 1
    elif fault == "TRANSITION_DUPLICATION":
        mutated.insert(stage_index, copy.deepcopy(mutated[stage_index]))
    elif fault == "SERVICE_CRASH":
        mutated[stage_index]["bot_active"] = False
    elif fault == "CHECKPOINT_STALENESS":
        mutated[stage_index]["checkpoint_fresh"] = False
    elif fault == "INVARIANT_DRIFT":
        mutated[stage_index]["invariants"]["live_execution"] = True
    elif fault == "KILL_SWITCH_LOSS":
        mutated[stage_index]["invariants"]["paper_kill_switch"] = False
    elif fault == "FALSE_HEALTH":
        mutated[stage_index]["transition"] = "HEALTHY"
    elif fault == "RESTART_STORM":
        mutated[stage_index]["restart_attempt"] = 4
    elif fault == "TELEMETRY_LOSS":
        del mutated[stage_index]["transition_sha256"]
    return mutated


def run_fault_campaign() -> dict[str, object]:
    pristine = build_passing_trace()
    rows = []
    silent = []
    for fault, stages in FAULTS.items():
        for stage_index in stages:
            first = certify_rehearsal(inject_fault(pristine, fault, stage_index))
            second = certify_rehearsal(inject_fault(pristine, fault, stage_index))
            expected_fragment = EXPECTED_CODES[fault]
            observed = any(expected_fragment in error for error in first["errors"])
            safe_refusal = (
                first["verdict"] == "REFUSE"
                and first["final_health"] == "UNHEALTHY_FROZEN"
                and first["executable"] is False
            )
            deterministic = first == second
            if not observed or not safe_refusal or not deterministic:
                silent.append(f"{fault}@{stage_index}")
            rows.append(
                {
                    "fault": fault,
                    "stage_index": stage_index,
                    "stage": pristine[stage_index]["transition"],
                    "expected_code_fragment": expected_fragment,
                    "observable": observed,
                    "safe_refusal": safe_refusal,
                    "deterministic": deterministic,
                    "certificate_sha256": first["certificate_sha256"],
                }
            )
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not silent else "REFUSE",
        "errors": [] if not silent else ["SILENT_OR_UNSAFE_FAULT"],
        "fault_classes": list(FAULTS),
        "case_count": len(rows),
        "silent_faults": silent,
        "zero_silent_invariant_violations": not silent,
        "results": rows,
        "final_state": "FROZEN",
        "actual_restart_performed": False,
        "executable": False,
        "safety": _safety(),
    }
    return {**body, "campaign_sha256": _digest(body)}


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
