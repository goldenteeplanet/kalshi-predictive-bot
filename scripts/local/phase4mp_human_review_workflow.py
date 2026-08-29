"""Hash-linked offline human-review lifecycle for verifier disagreement closure."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

SCHEMA = "phase4mp.human-review-workflow-validation.v1"
EVENT_SCHEMA = "phase4mp.human-review-event.v1"
CLOSURE_SCHEMA = "phase4mp.disagreement-closure-certificate.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
EVENT_FIELDS = {
    "schema",
    "event_id",
    "workflow_id",
    "review_epoch",
    "sequence",
    "operation",
    "actor_id",
    "occurred_at",
    "packet_sha256",
    "implementation_identity_sha256",
    "payload",
    "previous_event_sha256",
    "event_sha256",
}
OPERATIONS = {
    "INTAKE",
    "ASSIGN_REVIEWERS",
    "INSPECT_EVIDENCE",
    "SELECT_FIX",
    "ATTACH_FIX_EVIDENCE",
    "REVERIFY_PACKAGE",
    "RERUN_VERIFIERS",
    "SIGN_OFF",
    "CLOSE",
    "REJECT",
    "EXPIRE",
    "WITHDRAW",
    "REOPEN",
}
TERMINAL = {"CLOSED", "REJECTED", "EXPIRED", "WITHDRAWN"}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def make_event(
    operation: str,
    *,
    workflow_id: str,
    review_epoch: int,
    sequence: int,
    actor_id: str,
    occurred_at: str,
    packet_sha256: str,
    implementation_identity_sha256: str,
    payload: dict[str, object],
    previous_event_sha256: str,
) -> dict[str, object]:
    identity = {
        "workflow_id": workflow_id,
        "review_epoch": review_epoch,
        "sequence": sequence,
        "operation": operation,
        "actor_id": actor_id,
    }
    body: dict[str, object] = {
        "schema": EVENT_SCHEMA,
        "event_id": _digest(identity),
        "workflow_id": workflow_id,
        "review_epoch": review_epoch,
        "sequence": sequence,
        "operation": operation,
        "actor_id": actor_id,
        "occurred_at": occurred_at,
        "packet_sha256": packet_sha256,
        "implementation_identity_sha256": implementation_identity_sha256,
        "payload": payload,
        "previous_event_sha256": previous_event_sha256,
    }
    return {**body, "event_sha256": _digest(body)}


def validate_workflow(
    events: object,
    *,
    expected_packet_sha256: str,
    expected_implementation_identity_sha256: str,
    evaluated_at: str,
) -> dict[str, object]:
    source_sha256 = _digest(events)
    errors: list[str] = []
    if not isinstance(events, list):
        errors.append("EVENTS_NOT_LIST")
        events = []
    now = _time(evaluated_at)
    if now is None:
        errors.append("EVALUATION_TIME_INVALID")
    state = "NEW"
    workflow_id = None
    epoch = 1
    head = "0" * 64
    previous_time: datetime | None = None
    requester = None
    reviewers: list[str] = []
    signoffs: dict[str, str] = {}
    pre_fix_evidence_sha256 = None
    fix_evidence_sha256 = None
    review_expires_at: datetime | None = None
    selected_fix = None
    post_fix_consensus_sha256 = None
    accepted = 0
    closure = None
    seen_event_ids: dict[str, str] = {}
    for index, candidate in enumerate(events):
        row_errors: list[str] = []
        if not isinstance(candidate, dict) or set(candidate) != EVENT_FIELDS:
            row_errors.append("FIELD_SET_INVALID")
            candidate = {}
        event_id = candidate.get("event_id")
        fingerprint = _digest(candidate)
        if HEX64.fullmatch(str(event_id)) is None:
            row_errors.append("EVENT_ID_INVALID")
        elif event_id in seen_event_ids:
            if seen_event_ids[str(event_id)] == fingerprint:
                row_errors.append("SIGNOFF_OR_EVENT_REPLAY")
            else:
                row_errors.append("CONFLICTING_EVENT_REPLAY")
        seen_event_ids[str(event_id)] = fingerprint
        body = {key: value for key, value in candidate.items() if key != "event_sha256"}
        if candidate.get("event_sha256") != _digest(body):
            row_errors.append("EVENT_HASH_INVALID")
        if candidate.get("previous_event_sha256") != head:
            row_errors.append("CHAIN_LINK_INVALID")
        if candidate.get("sequence") != accepted + 1:
            row_errors.append("SEQUENCE_INVALID_OR_SKIPPED")
        if candidate.get("packet_sha256") != expected_packet_sha256:
            row_errors.append("PACKET_SUBSTITUTION")
        if (
            candidate.get("implementation_identity_sha256")
            != expected_implementation_identity_sha256
        ):
            row_errors.append("IMPLEMENTATION_IDENTITY_SUBSTITUTION")
        if workflow_id is None:
            workflow_id = candidate.get("workflow_id")
        elif candidate.get("workflow_id") != workflow_id:
            row_errors.append("WORKFLOW_SUBSTITUTION")
        operation = candidate.get("operation")
        if operation not in OPERATIONS:
            row_errors.append("OPERATION_INVALID")
        occurred = _time(candidate.get("occurred_at"))
        if occurred is None:
            row_errors.append("TIME_INVALID")
        elif now is not None and occurred > now:
            row_errors.append("EVENT_FROM_FUTURE")
        elif previous_time is not None and occurred < previous_time:
            row_errors.append("TIME_REVERSED")
        payload = candidate.get("payload")
        if not isinstance(payload, dict):
            row_errors.append("PAYLOAD_INVALID")
            payload = {}
        actor = candidate.get("actor_id")
        if not isinstance(actor, str) or not actor:
            row_errors.append("ACTOR_INVALID")
        event_epoch = candidate.get("review_epoch")
        if operation == "REOPEN":
            if state != "CLOSED" or type(event_epoch) is not int or event_epoch != epoch + 1:
                row_errors.append("REOPEN_REQUIRES_NEW_EPOCH")
        elif event_epoch != epoch:
            row_errors.append("REVIEW_EPOCH_INVALID")
        if operation == "INTAKE":
            if state != "NEW":
                row_errors.append("TRANSITION_INVALID")
            requester = payload.get("requester_id")
            pre_fix_evidence_sha256 = payload.get("pre_fix_evidence_sha256")
            review_expires_at = _time(payload.get("review_expires_at"))
            if requester != actor or HEX64.fullmatch(str(pre_fix_evidence_sha256)) is None:
                row_errors.append("INTAKE_BINDING_INVALID")
            if review_expires_at is None or (
                occurred is not None and review_expires_at <= occurred
            ):
                row_errors.append("REVIEW_EXPIRY_INVALID")
        elif operation == "ASSIGN_REVIEWERS":
            if state != "INTAKEN":
                row_errors.append("TRANSITION_INVALID")
            value = payload.get("reviewers")
            if (
                not isinstance(value, list)
                or len(value) != 2
                or len(set(value)) != 2
                or any(not isinstance(item, str) or not item for item in value)
                or requester in value
            ):
                row_errors.append("SELF_OR_DUPLICATE_REVIEWER")
            else:
                reviewers = value
        elif operation == "INSPECT_EVIDENCE":
            if state != "ASSIGNED" or actor not in reviewers:
                row_errors.append("TRANSITION_OR_REVIEWER_INVALID")
            if payload.get("pre_fix_evidence_sha256") != pre_fix_evidence_sha256:
                row_errors.append("PRE_FIX_EVIDENCE_MUTATED")
        elif operation == "SELECT_FIX":
            if state != "INSPECTED" or actor not in reviewers:
                row_errors.append("TRANSITION_OR_REVIEWER_INVALID")
            selected_fix = payload.get("selection")
            if selected_fix not in {"FIX_PRIMARY", "FIX_SECONDARY", "FIX_BOTH"}:
                row_errors.append("FIX_SELECTION_INVALID")
        elif operation == "ATTACH_FIX_EVIDENCE":
            if state != "FIX_SELECTED":
                row_errors.append("TRANSITION_INVALID")
            fix_evidence_sha256 = payload.get("fix_evidence_sha256")
            if (
                HEX64.fullmatch(str(fix_evidence_sha256)) is None
                or payload.get("selection") != selected_fix
            ):
                row_errors.append("FIX_EVIDENCE_STALE_OR_UNBOUND")
        elif operation == "REVERIFY_PACKAGE":
            if state != "FIX_ATTACHED":
                row_errors.append("TRANSITION_INVALID")
            if (
                payload.get("fix_evidence_sha256") != fix_evidence_sha256
                or payload.get("package_verdict") != "PASS"
                or HEX64.fullmatch(str(payload.get("verification_sha256"))) is None
            ):
                row_errors.append("PACKAGE_REVERIFICATION_INVALID")
        elif operation == "RERUN_VERIFIERS":
            if state != "REVERIFIED":
                row_errors.append("TRANSITION_INVALID")
            if (
                payload.get("primary_verdict") != "PASS"
                or payload.get("secondary_verdict") != "PASS"
                or payload.get("first_differing_semantic_field") is not None
                or payload.get("implementation_identity_sha256")
                != expected_implementation_identity_sha256
            ):
                row_errors.append("POST_FIX_CONSENSUS_INVALID")
            post_fix_consensus_sha256 = payload.get("consensus_sha256")
            if HEX64.fullmatch(str(post_fix_consensus_sha256)) is None:
                row_errors.append("POST_FIX_CONSENSUS_HASH_INVALID")
        elif operation == "SIGN_OFF":
            if state not in {"CONSENSUS_PASS", "ONE_SIGNOFF"} or actor not in reviewers:
                row_errors.append("TRANSITION_OR_REVIEWER_INVALID")
            if actor in signoffs:
                row_errors.append("REPLAYED_SIGNOFF")
            context = _digest(
                {
                    "packet_sha256": expected_packet_sha256,
                    "fix_evidence_sha256": fix_evidence_sha256,
                    "post_fix_consensus_sha256": post_fix_consensus_sha256,
                    "review_epoch": epoch,
                }
            )
            if payload.get("closure_context_sha256") != context:
                row_errors.append("SIGNOFF_CONTEXT_INVALID")
            signoffs[str(actor)] = str(candidate.get("event_sha256"))
        elif operation == "CLOSE":
            if state != "SIGNED" or len(signoffs) != 2:
                row_errors.append("DUAL_SIGNOFF_REQUIRED")
            if payload.get("post_fix_consensus_sha256") != post_fix_consensus_sha256:
                row_errors.append("CLOSURE_CONSENSUS_SUBSTITUTED")
        elif operation in {"REJECT", "WITHDRAW", "EXPIRE"}:
            if state in TERMINAL or state == "NEW":
                row_errors.append("TERMINAL_TRANSITION_INVALID")
        elif operation == "REOPEN":
            if (
                payload.get("new_evidence_sha256") == pre_fix_evidence_sha256
                or HEX64.fullmatch(str(payload.get("new_evidence_sha256"))) is None
            ):
                row_errors.append("REOPEN_NEW_EVIDENCE_REQUIRED")
        if state in TERMINAL and operation != "REOPEN":
            row_errors.append("POST_TERMINAL_MUTATION")
        if review_expires_at is not None and occurred is not None and occurred >= review_expires_at:
            if operation not in {"EXPIRE", "REOPEN"}:
                row_errors.append("REVIEW_EXPIRED")
        if row_errors:
            errors.extend(f"EVENT_{index}_{error}" for error in sorted(set(row_errors)))
            break
        transitions = {
            "INTAKE": "INTAKEN",
            "ASSIGN_REVIEWERS": "ASSIGNED",
            "INSPECT_EVIDENCE": "INSPECTED",
            "SELECT_FIX": "FIX_SELECTED",
            "ATTACH_FIX_EVIDENCE": "FIX_ATTACHED",
            "REVERIFY_PACKAGE": "REVERIFIED",
            "RERUN_VERIFIERS": "CONSENSUS_PASS",
            "REJECT": "REJECTED",
            "EXPIRE": "EXPIRED",
            "WITHDRAW": "WITHDRAWN",
        }
        if operation == "SIGN_OFF":
            state = "SIGNED" if len(signoffs) == 2 else "ONE_SIGNOFF"
        elif operation == "CLOSE":
            state = "CLOSED"
            closure_body: dict[str, object] = {
                "schema": CLOSURE_SCHEMA,
                "workflow_id": workflow_id,
                "review_epoch": epoch,
                "packet_sha256": expected_packet_sha256,
                "implementation_identity_sha256": expected_implementation_identity_sha256,
                "fix_evidence_sha256": fix_evidence_sha256,
                "post_fix_consensus_sha256": post_fix_consensus_sha256,
                "reviewer_signoff_event_sha256": [signoffs[key] for key in sorted(signoffs)],
                "disagreement_resolved": True,
                "package_acceptance_authorized": False,
                "repair_execution_authorized": False,
                "order_capability": False,
            }
            closure = {**closure_body, "closure_sha256": _digest(closure_body)}
        elif operation == "REOPEN":
            epoch = int(event_epoch)
            state = "INTAKEN"
            pre_fix_evidence_sha256 = payload["new_evidence_sha256"]
            reviewers = []
            signoffs = {}
            selected_fix = None
            fix_evidence_sha256 = None
            post_fix_consensus_sha256 = None
            closure = None
        else:
            state = transitions.get(str(operation), state)
        accepted += 1
        head = str(candidate.get("event_sha256"))
        previous_time = occurred
    if (
        review_expires_at is not None
        and now is not None
        and now >= review_expires_at
        and state not in TERMINAL
    ):
        errors.append("REVIEW_EXPIRED_UNCLOSED")
    errors = sorted(set(errors))
    residual_risks = [
        "closure proves disagreement resolution only",
        "package acceptance remains separately prohibited",
        "repair and trading execution remain unavailable",
    ]
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "state": state,
        "review_epoch": epoch,
        "accepted_event_count": accepted,
        "head_event_sha256": head,
        "source_sha256": source_sha256,
        "source_unchanged": _digest(events) == source_sha256,
        "closure_certificate": closure,
        "residual_risks": residual_risks,
        "residual_risk_sha256": _digest(residual_risks),
        "safety": {
            "simulation_only": True,
            "workflow_persistence": False,
            "package_acceptance": False,
            "repair_execution": False,
            "network_access": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["lifecycle_sha256"] = _digest(result)
    return result
