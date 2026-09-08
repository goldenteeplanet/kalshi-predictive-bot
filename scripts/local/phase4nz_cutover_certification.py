"""Canonical cutover evidence bundle and independent ceremony certification."""

from __future__ import annotations

import copy
import hashlib
import json

SCHEMA = "phase4nz.cutover-evidence.v1"
CEREMONY_STEPS = (
    "OBSERVE_WITNESS_STATE",
    "READ_BACK_ACTION",
    "ACKNOWLEDGE_CHECKLIST",
    "VERIFY_CHECKPOINT",
    "CONFIRM_QUORUM",
    "CONFIRM_ROLLBACK",
    "EXECUTE_SIMULATION",
    "POST_STAGE_VERIFY",
)
REQUIRED_FAULTS = {
    "SKIPPED_STEP",
    "OUT_OF_ORDER",
    "WRONG_WITNESS",
    "STALE_OBSERVATION",
    "INCORRECT_READBACK",
    "MISMATCHED_PLAN",
    "PREMATURE_REVOCATION",
    "THRESHOLD_MISTAKE",
    "AMBIGUOUS_RESPONSE",
    "FAILED_VERIFICATION",
    "TIMEOUT",
    "INTERRUPTION",
    "CONTINUE_AFTER_ROLLBACK",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def create_evidence_bundle(
    *,
    plan: dict[str, object],
    authorization_request: dict[str, object],
    approvals: list[dict[str, object]],
    ceremony: dict[str, object],
    ceremony_result: dict[str, object],
    failure_evidence: list[dict[str, object]],
) -> dict[str, object]:
    artifacts = copy.deepcopy(
        {
            "plan": plan,
            "authorization_request": authorization_request,
            "approvals": approvals,
            "ceremony": ceremony,
            "ceremony_result": ceremony_result,
            "checkpoint_anchor": ceremony["checkpoint_anchor"],
            "quorum_anchor": ceremony["quorum_sha256"],
            "rollback_anchor": ceremony["rollback_sha256"],
            "placement_evidence_hashes": authorization_request["placement_evidence_hashes"],
            "operator_identities": ceremony["roles"],
            "stage_outcomes": [
                {"stage": row["stage"], "transcript_sha256": row["transcript_sha256"]}
                for row in ceremony_result["transcript"]
                if row["step"] == "POST_STAGE_VERIFY"
            ],
            "failure_evidence": failure_evidence,
            "safety": _safety(),
        }
    )
    # Break source-object aliasing so each manifest entry is independently immutable.
    artifacts["operator_identities"] = copy.deepcopy(ceremony["roles"])
    artifacts["placement_evidence_hashes"] = copy.deepcopy(
        authorization_request["placement_evidence_hashes"]
    )
    manifest = {
        name: {
            "sha256": _digest(value),
            "canonical_bytes": len(
                json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
            ),
        }
        for name, value in artifacts.items()
    }
    result = {"schema": SCHEMA, "artifacts": artifacts, "manifest": manifest}
    result["bundle_sha256"] = _digest(result)
    return result


def independently_verify_bundle(
    bundle: object, *, reviewer_registry: dict[str, object]
) -> dict[str, object]:
    errors = []
    required = {
        "plan",
        "authorization_request",
        "approvals",
        "ceremony",
        "ceremony_result",
        "checkpoint_anchor",
        "quorum_anchor",
        "rollback_anchor",
        "placement_evidence_hashes",
        "operator_identities",
        "stage_outcomes",
        "failure_evidence",
        "safety",
    }
    if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA:
        return _result(["EVIDENCE_SCHEMA_INVALID"], None)
    artifacts, manifest = bundle.get("artifacts"), bundle.get("manifest")
    if not isinstance(artifacts, dict) or not isinstance(manifest, dict):
        return _result(["EVIDENCE_SHAPE_INVALID"], None)
    if required - set(artifacts):
        errors.append("EVIDENCE_MISSING")
    for name in required & set(artifacts):
        entry = manifest.get(name)
        if not isinstance(entry, dict) or entry.get("sha256") != _digest(artifacts[name]):
            errors.append("EVIDENCE_HASH_MISMATCH")
    unsigned_bundle = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    if bundle.get("bundle_sha256") != _digest(unsigned_bundle):
        errors.append("EVIDENCE_BUNDLE_HASH_MISMATCH")
    if errors:
        return _result(sorted(set(errors)), bundle.get("bundle_sha256"))

    plan = artifacts["plan"]
    request = artifacts["authorization_request"]
    approvals = artifacts["approvals"]
    ceremony = artifacts["ceremony"]
    ceremony_result = artifacts["ceremony_result"]
    plan_body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("plan_sha256") != _digest(plan_body):
        errors.append("PLAN_HASH_MISMATCH")
    request_body = {key: value for key, value in request.items() if key != "request_sha256"}
    if request.get("request_sha256") != _digest(request_body):
        errors.append("APPROVAL_REQUEST_DRIFT")
    if request.get("plan_sha256") != plan.get("plan_sha256"):
        errors.append("APPROVAL_PLAN_DRIFT")
    start, end = request.get("stage_start"), request.get("stage_end")
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or not (0 <= start <= end < len(plan["actions"]))
    ):
        errors.append("UNAUTHORIZED_STAGE")
        expected_scope_hash = None
    else:
        expected_scope_hash = _digest(plan["actions"][start : end + 1])
    if request.get("stage_actions_sha256") != expected_scope_hash:
        errors.append("UNAUTHORIZED_STAGE")
    reviewers, domains = set(), set()
    for approval in approvals:
        reviewer = reviewer_registry.get(approval.get("reviewer_id"))
        if not isinstance(reviewer, dict) or reviewer.get("status") != "AUTHORIZED":
            errors.append("APPROVAL_REVIEWER_INVALID")
            continue
        body = {key: value for key, value in approval.items() if key != "signature_sha256"}
        if approval.get("signature_sha256") != _digest({"key": reviewer["key"], "approval": body}):
            errors.append("APPROVAL_SIGNATURE_DRIFT")
        if (
            approval.get("request_sha256") != request.get("request_sha256")
            or approval.get("stage_start") != start
            or approval.get("stage_end") != end
        ):
            errors.append("APPROVAL_SCOPE_DRIFT")
        reviewers.add(approval.get("reviewer_id"))
        domains.add(reviewer.get("domain"))
    if len(reviewers) < 2 or len(domains) < 2:
        errors.append("APPROVAL_INDEPENDENCE_MISSING")
    roles = ceremony.get("roles", {})
    if len(set(roles.values())) != 3 or artifacts["operator_identities"] != roles:
        errors.append("ROLE_COLLISION")
    ceremony_body = {key: value for key, value in ceremony.items() if key != "ceremony_sha256"}
    if ceremony.get("ceremony_sha256") != _digest(ceremony_body):
        errors.append("CEREMONY_HASH_MISMATCH")
    if (
        artifacts["checkpoint_anchor"] != ceremony.get("checkpoint_anchor")
        or artifacts["quorum_anchor"] != ceremony.get("quorum_sha256")
        or artifacts["rollback_anchor"] != ceremony.get("rollback_sha256")
    ):
        errors.append("ANCHOR_MISMATCH")
    expected_events = (
        [(stage, step) for stage in range(start, end + 1) for step in CEREMONY_STEPS]
        if isinstance(start, int) and isinstance(end, int)
        else []
    )
    actual_events = [(row.get("stage"), row.get("step")) for row in ceremony.get("events", [])]
    if actual_events != expected_events:
        errors.append("CEREMONY_ORDER_MISMATCH")
    for event in ceremony.get("events", []):
        body = {key: value for key, value in event.items() if key != "event_sha256"}
        if event.get("event_sha256") != _digest(body):
            errors.append("CEREMONY_EVENT_HASH_MISMATCH")
    result_body = {
        key: value for key, value in ceremony_result.items() if key != "transcript_root_sha256"
    }
    if ceremony_result.get("transcript_root_sha256") != _digest(result_body):
        errors.append("TRANSCRIPT_ROOT_MISMATCH")
    transcript = ceremony_result.get("transcript", [])
    for index, row in enumerate(transcript):
        body = {key: value for key, value in row.items() if key != "transcript_sha256"}
        if row.get("transcript_sha256") != _digest(body):
            errors.append("TRANSCRIPT_HASH_MISMATCH")
        if index and row.get("previous_transcript_sha256") != transcript[index - 1].get(
            "transcript_sha256"
        ):
            errors.append("TRANSCRIPT_CHAIN_MISMATCH")
    if (
        ceremony_result.get("verdict") != "PASS"
        or ceremony_result.get("terminal_state") != "COMPLETED"
    ):
        errors.append("CEREMONY_NONTERMINAL")
    if ceremony_result.get("completed_stages") != end - start + 1:
        errors.append("CEREMONY_STAGE_INCOMPLETE")
    expected_outcomes = [
        {"stage": row["stage"], "transcript_sha256": row["transcript_sha256"]}
        for row in transcript
        if row.get("step") == "POST_STAGE_VERIFY"
    ]
    if artifacts["stage_outcomes"] != expected_outcomes:
        errors.append("STAGE_OUTCOME_MISMATCH")
    fault_rows = artifacts["failure_evidence"]
    fault_names = {row.get("fault") for row in fault_rows}
    if fault_names != REQUIRED_FAULTS or any(
        row.get("verdict") != "REFUSE" or row.get("terminal_state") not in {"PAUSED", "ROLLED_BACK"}
        for row in fault_rows
    ):
        errors.append("FAULT_COVERAGE_INCOMPLETE")
    if not artifacts["rollback_anchor"]:
        errors.append("ROLLBACK_AMBIGUITY")
    for safety in (artifacts["safety"], ceremony_result.get("safety"), request.get("safety")):
        if safety != _safety():
            errors.append("UNSAFE_CAPABILITY_STATE")
    reconstruction = {
        "plan_sha256": plan.get("plan_sha256"),
        "request_sha256": request.get("request_sha256"),
        "ceremony_sha256": ceremony.get("ceremony_sha256"),
        "transcript_root_sha256": ceremony_result.get("transcript_root_sha256"),
        "faults": sorted(fault_names),
    }
    return _result(sorted(set(errors)), bundle["bundle_sha256"], reconstruction)


def _result(errors, bundle_sha256, reconstruction=None):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "bundle_sha256": bundle_sha256,
        "reconstruction": reconstruction,
        "safety": _safety(),
    }
    result["certification_sha256"] = _digest(result)
    return result


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
