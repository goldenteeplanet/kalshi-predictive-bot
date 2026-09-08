"""End-to-end disaster-recovery chaos matrix and logical RTO proof."""

from __future__ import annotations

import hashlib
import json

from scripts.local.phase4oa_aggregate_release_gate import BLOCKED_ON_SETTLEMENT

SCHEMA = "phase4ol.disaster-recovery-chaos.v1"
SCENARIOS = (
    ("QUORUM_LOSS", 2, "FROZEN"),
    ("FREEZE_COMMIT", 2, "FROZEN"),
    ("RESTART_REPLAY", 4, "FROZEN"),
    ("CHECKPOINT_LOSS", 5, "FROZEN"),
    ("SINGLE_COPY_CORRUPTION", 6, "FROZEN"),
    ("ANCHOR_UNAVAILABLE", 3, "FROZEN"),
    ("WITNESS_EQUIVOCATION", 3, "FROZEN"),
    ("SAFE_COPY_REPAIR", 6, "FROZEN"),
    ("AUTHORIZED_RECOVERY", 7, "RECOVERED"),
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def record_scenario(
    *,
    scenario: str,
    logical_steps: int,
    state: str,
    capabilities_allowed: bool,
    proof_sha256: str,
) -> dict[str, object]:
    body = {
        "schema": SCHEMA,
        "scenario": scenario,
        "logical_steps": logical_steps,
        "state": state,
        "capabilities_allowed": capabilities_allowed,
        "proof_sha256": proof_sha256,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "record_sha256": _digest(body)}


def certify_chaos_matrix(records: list[dict[str, object]]) -> dict[str, object]:
    errors: list[str] = []
    expected_names = [row[0] for row in SCENARIOS]
    actual_names = [row.get("scenario") for row in records]
    if actual_names != expected_names:
        errors.append("SCENARIO_SEQUENCE_INCOMPLETE_OR_REORDERED")
    normalized = []
    for index, specification in enumerate(SCENARIOS):
        if index >= len(records):
            continue
        record = records[index]
        scenario, maximum_steps, expected_state = specification
        unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
        if record.get("record_sha256") != _digest(unsigned):
            errors.append(f"{scenario}:RECORD_HASH_MISMATCH")
        if record.get("schema") != SCHEMA:
            errors.append(f"{scenario}:SCHEMA_INVALID")
        steps = record.get("logical_steps")
        if not isinstance(steps, int) or steps < 1 or steps > maximum_steps:
            errors.append(f"{scenario}:RTO_BUDGET_EXCEEDED")
        if record.get("state") != expected_state:
            errors.append(f"{scenario}:STATE_TRANSITION_INVALID")
        if index < len(SCENARIOS) - 1 and record.get("capabilities_allowed") is not False:
            errors.append(f"{scenario}:DEGRADED_CAPABILITY_EXPOSURE")
        if scenario == "AUTHORIZED_RECOVERY" and record.get("capabilities_allowed") is not True:
            errors.append("AUTHORIZED_RECOVERY:CAPABILITY_NOT_RESTORED")
        if not record.get("proof_sha256"):
            errors.append(f"{scenario}:PROOF_MISSING")
        if record.get("blocked_on_september_1_settlement") != BLOCKED_ON_SETTLEMENT:
            errors.append(f"{scenario}:SETTLEMENT_BLOCKER_DRIFT")
        if record.get("safety") != _safety():
            errors.append(f"{scenario}:SAFETY_INVARIANT_VIOLATION")
        normalized.append(record)
    total_steps = sum(
        record.get("logical_steps", 0)
        for record in records
        if isinstance(record.get("logical_steps"), int)
    )
    total_budget = sum(row[1] for row in SCENARIOS)
    if total_steps > total_budget:
        errors.append("AGGREGATE_RTO_BUDGET_EXCEEDED")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "scenario_count": len(normalized),
        "scenario_order": expected_names,
        "total_logical_steps": total_steps,
        "total_step_budget": total_budget,
        "records": normalized,
        "final_state": "RECOVERED" if not errors else "FROZEN",
        "capabilities_allowed": not errors,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "certificate_sha256": _digest(body)}


def build_passing_matrix() -> list[dict[str, object]]:
    return [
        record_scenario(
            scenario=scenario,
            logical_steps=maximum_steps,
            state=state,
            capabilities_allowed=scenario == "AUTHORIZED_RECOVERY",
            proof_sha256=_digest({"scenario": scenario, "outcome": state}),
        )
        for scenario, maximum_steps, state in SCENARIOS
    ]


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
