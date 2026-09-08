"""In-memory CAS, race, and crash simulator for Phase 4MP review workflows."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4mp_human_review_workflow import validate_workflow

SCHEMA = "phase4mq.review-workflow-concurrency-simulation.v1"
CRASH_SCHEMA = "phase4mq.review-workflow-crash-matrix.v1"
PROPOSAL_SCHEMA = "phase4mq.review-event-append-proposal.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def make_proposal(
    event: dict[str, object],
    *,
    expected_generation: int,
    expected_head_sha256: str,
    durability: str = "DURABLE",
    proposal_id: str | None = None,
) -> dict[str, object]:
    identity = {
        "event_sha256": event.get("event_sha256"),
        "expected_generation": expected_generation,
        "expected_head_sha256": expected_head_sha256,
        "durability": durability,
    }
    return {
        "schema": PROPOSAL_SCHEMA,
        "proposal_id": proposal_id or _digest(identity),
        "expected_generation": expected_generation,
        "expected_head_sha256": expected_head_sha256,
        "durability": durability,
        "event": event,
    }


def simulate_cas(
    durable_events: object,
    proposals: object,
    *,
    expected_packet_sha256: str,
    expected_implementation_identity_sha256: str,
    evaluated_at: str,
) -> dict[str, object]:
    source_sha256 = _digest(durable_events)
    errors: list[str] = []
    if not isinstance(durable_events, list) or not isinstance(proposals, list):
        errors.append("SOURCE_OR_PROPOSALS_INVALID")
        durable_events, proposals = [], []
    history = copy.deepcopy(durable_events)
    baseline = validate_workflow(
        history,
        expected_packet_sha256=expected_packet_sha256,
        expected_implementation_identity_sha256=expected_implementation_identity_sha256,
        evaluated_at=evaluated_at,
    )
    if baseline["verdict"] != "PASS":
        errors.append("BASE_HISTORY_INVALID")
    generation = len(history)
    head = history[-1]["event_sha256"] if history else "0" * 64
    proposal_identities: dict[str, str] = {}
    outcomes: list[dict[str, object]] = []
    accepted_count = 0
    for index, proposal in enumerate(proposals):
        outcome = "REFUSED"
        reason = None
        if not isinstance(proposal, dict) or set(proposal) != {
            "schema",
            "proposal_id",
            "expected_generation",
            "expected_head_sha256",
            "durability",
            "event",
        }:
            reason = "PROPOSAL_SHAPE_INVALID"
        elif proposal.get("schema") != PROPOSAL_SCHEMA:
            reason = "PROPOSAL_SCHEMA_INVALID"
        else:
            proposal_id = str(proposal.get("proposal_id"))
            fingerprint = _digest(proposal)
            if proposal_id in proposal_identities:
                if proposal_identities[proposal_id] == fingerprint:
                    outcome = "IDEMPOTENT_RETRY"
                    reason = "EXACT_PROPOSAL_ALREADY_PROCESSED"
                else:
                    reason = "CONFLICTING_RETRY"
            else:
                proposal_identities[proposal_id] = fingerprint
                if proposal.get("durability") == "PREPARED":
                    outcome = "RECOVERY_DISCARD"
                    reason = "PREPARED_EVENT_NEVER_DURABLE"
                elif proposal.get("durability") != "DURABLE":
                    reason = "DURABILITY_INVALID"
                elif (
                    proposal.get("expected_generation") != generation
                    or proposal.get("expected_head_sha256") != head
                ):
                    reason = "CAS_GENERATION_OR_HEAD_MISMATCH"
                else:
                    candidate = history + [copy.deepcopy(proposal.get("event"))]
                    validation = validate_workflow(
                        candidate,
                        expected_packet_sha256=expected_packet_sha256,
                        expected_implementation_identity_sha256=expected_implementation_identity_sha256,
                        evaluated_at=evaluated_at,
                    )
                    if validation["verdict"] != "PASS":
                        reason = "WORKFLOW_TRANSITION_INVALID"
                    else:
                        history = candidate
                        generation += 1
                        head = history[-1]["event_sha256"]
                        accepted_count += 1
                        outcome = "ACCEPTED"
                        reason = "LINEARIZED_AT_EXACT_CAS_POINT"
        outcomes.append(
            {
                "proposal_index": index,
                "proposal_id": proposal.get("proposal_id") if isinstance(proposal, dict) else None,
                "outcome": outcome,
                "reason": reason,
                "generation_after": generation,
                "head_after": head,
            }
        )
    final_validation = validate_workflow(
        history,
        expected_packet_sha256=expected_packet_sha256,
        expected_implementation_identity_sha256=expected_implementation_identity_sha256,
        evaluated_at=evaluated_at,
    )
    if final_validation["verdict"] != "PASS":
        errors.append("FINAL_HISTORY_INVALID")
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "initial_generation": len(durable_events),
        "final_generation": generation,
        "final_head_sha256": head,
        "accepted_count": accepted_count,
        "outcomes": outcomes,
        "durable_events": history,
        "durable_history_sha256": _digest(history),
        "source_unchanged": _digest(durable_events) == source_sha256,
        "final_workflow_state": final_validation["state"],
        "safety": {
            "simulation_only": True,
            "workflow_persistence": False,
            "network_access": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["linearization_sha256"] = _digest(result)
    return result


def crash_recovery_matrix(
    events: object,
    *,
    expected_packet_sha256: str,
    expected_implementation_identity_sha256: str,
    evaluated_at: str,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(events, list):
        events = []
        errors.append("EVENTS_INVALID")
    records: list[dict[str, object]] = []
    for durable_count in range(len(events) + 1):
        prefix = events[:durable_count]
        validation = validate_workflow(
            prefix,
            expected_packet_sha256=expected_packet_sha256,
            expected_implementation_identity_sha256=expected_implementation_identity_sha256,
            evaluated_at=evaluated_at,
        )
        if validation["verdict"] != "PASS":
            errors.append(f"PREFIX_{durable_count}_INVALID")
        prepared_event = events[durable_count] if durable_count < len(events) else None
        records.append(
            {
                "crash_point": durable_count,
                "durable_count": durable_count,
                "durable_head_sha256": prefix[-1]["event_sha256"] if prefix else "0" * 64,
                "resumed_state": validation["state"],
                "prepared_event_sha256": (
                    prepared_event.get("event_sha256") if isinstance(prepared_event, dict) else None
                ),
                "prepared_recovery_action": (
                    "DISCARD_PREPARED_FAIL_CLOSED"
                    if prepared_event is not None
                    else "NO_PREPARED_EVENT"
                ),
                "durable_events_lost": 0,
                "resume_from_exact_head": True,
            }
        )
    result: dict[str, object] = {
        "schema": CRASH_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "crash_point_count": len(records),
        "records": records,
        "coverage_sha256": _digest(records),
        "simulation_only": True,
    }
    result["recovery_sha256"] = _digest(result)
    return result


def audit_interleavings(
    scenarios: object,
    *,
    expected_packet_sha256: str,
    expected_implementation_identity_sha256: str,
    evaluated_at: str,
) -> dict[str, object]:
    errors: list[str] = []
    records: list[dict[str, object]] = []
    if not isinstance(scenarios, list):
        scenarios = []
        errors.append("SCENARIOS_INVALID")
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != {"name", "base_events", "proposals"}:
            errors.append("SCENARIO_SHAPE_INVALID")
            continue
        result = simulate_cas(
            scenario["base_events"],
            scenario["proposals"],
            expected_packet_sha256=expected_packet_sha256,
            expected_implementation_identity_sha256=expected_implementation_identity_sha256,
            evaluated_at=evaluated_at,
        )
        conflicting_winners = result["accepted_count"]
        if result["verdict"] != "PASS" or conflicting_winners > 1:
            errors.append(f"SCENARIO_{scenario['name']}_LINEARIZATION_FAILED")
        records.append(
            {
                "name": scenario["name"],
                "accepted_count": conflicting_winners,
                "final_state": result["final_workflow_state"],
                "linearization_sha256": result["linearization_sha256"],
            }
        )
    residual_risks = [
        "simulation does not provide durable production storage",
        "distributed clock and process failures remain outside this model",
        "workflow closure still grants no operational capability",
    ]
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors and records else "REFUSE",
        "errors": errors,
        "scenario_count": len(records),
        "records": records,
        "interleaving_coverage_sha256": _digest(records),
        "residual_risks": residual_risks,
        "residual_risk_sha256": _digest(residual_risks),
        "simulation_only": True,
    }
    result["audit_sha256"] = _digest(result)
    return result
