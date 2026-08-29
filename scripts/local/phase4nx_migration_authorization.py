"""Canonical two-person authorization for exact offline migration simulation stages."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

SCHEMA = "phase4nx.migration-authorization.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(UTC)


def reviewer_registry() -> dict[str, object]:
    return {
        "reviewer-a": {"status": "AUTHORIZED", "key": "reviewer-a-key", "domain": "security"},
        "reviewer-b": {"status": "AUTHORIZED", "key": "reviewer-b-key", "domain": "operations"},
        "reviewer-c": {"status": "AUTHORIZED", "key": "reviewer-c-key", "domain": "risk"},
        "reviewer-revoked": {"status": "REVOKED", "key": "revoked-key", "domain": "legacy"},
    }


def create_authorization_request(
    plan: dict[str, object],
    *,
    stage_start: int,
    stage_end: int,
    source_policy: dict[str, object],
    target_policy: dict[str, object],
    witness_identities: list[str],
    placement_evidence_hashes: list[str],
    checkpoint_anchor: str,
    rollback_point_hashes: list[str],
    issued_at: str,
    expires_at: str,
    nonce: str,
    requester_id: str,
    authorization_epoch: int,
) -> dict[str, object]:
    errors = []
    actions = plan.get("actions", [])
    if not (0 <= stage_start <= stage_end < len(actions)):
        errors.append("STAGE_RANGE_INVALID")
        stage_actions = []
    else:
        stage_actions = actions[stage_start : stage_end + 1]
    try:
        if _time(issued_at) >= _time(expires_at):
            errors.append("AUTHORIZATION_WINDOW_INVALID")
    except (TypeError, ValueError):
        errors.append("AUTHORIZATION_TIME_INVALID")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "plan_sha256": plan.get("plan_sha256"),
        "stage_start": stage_start,
        "stage_end": stage_end,
        "stage_actions_sha256": _digest(stage_actions),
        "source_policy": source_policy,
        "target_policy": target_policy,
        "witness_identities": sorted(witness_identities),
        "placement_evidence_hashes": sorted(placement_evidence_hashes),
        "checkpoint_anchor": checkpoint_anchor,
        "rollback_point_hashes": sorted(rollback_point_hashes),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce,
        "requester_id": requester_id,
        "authorization_epoch": authorization_epoch,
        "safety": _safety(),
    }
    body["request_sha256"] = _digest(body)
    return body


def approve_request(
    request: dict[str, object],
    *,
    reviewer_id: str,
    approved_at: str,
    registry: dict[str, object],
) -> dict[str, object]:
    reviewer = registry[reviewer_id]
    body = {
        "schema": SCHEMA,
        "request_sha256": request["request_sha256"],
        "reviewer_id": reviewer_id,
        "reviewer_domain": reviewer["domain"],
        "stage_start": request["stage_start"],
        "stage_end": request["stage_end"],
        "approved_at": approved_at,
        "nonce": request["nonce"],
    }
    return {**body, "signature_sha256": _digest({"key": reviewer["key"], "approval": body})}


def revoke_approval(
    request: dict[str, object], approval: dict[str, object], *, reason: str
) -> dict[str, object]:
    body = {
        "request_sha256": request["request_sha256"],
        "signature_sha256": approval["signature_sha256"],
        "reviewer_id": approval["reviewer_id"],
        "reason": reason,
    }
    return {**body, "revocation_sha256": _digest(body)}


def verify_authorization(
    request: dict[str, object],
    approvals: list[dict[str, object]],
    *,
    registry: dict[str, object],
    context: dict[str, object],
    now: str,
    used_authorization_ids: set[str] | None = None,
    revocations: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    errors = []
    unsigned = {key: value for key, value in request.items() if key != "request_sha256"}
    if request.get("request_sha256") != _digest(unsigned) or request.get("verdict") != "PASS":
        errors.append("AUTHORIZATION_REQUEST_INVALID")
    bindings = {
        "plan_sha256": "PLAN_DRIFT",
        "source_policy": "SOURCE_POLICY_DRIFT",
        "target_policy": "TARGET_POLICY_DRIFT",
        "witness_identities": "WITNESS_SUBSTITUTION",
        "placement_evidence_hashes": "PLACEMENT_EVIDENCE_DRIFT",
        "checkpoint_anchor": "CHECKPOINT_ANCHOR_DRIFT",
        "rollback_point_hashes": "ROLLBACK_POINT_DRIFT",
        "authorization_epoch": "AUTHORIZATION_EPOCH_DRIFT",
    }
    for field, code in bindings.items():
        expected = sorted(context[field]) if isinstance(context[field], list) else context[field]
        if request.get(field) != expected:
            errors.append(code)
    actions = context.get("actions", [])
    start, end = request.get("stage_start"), request.get("stage_end")
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or not (0 <= start <= end < len(actions))
    ):
        errors.append("PARTIAL_PLAN_APPROVAL")
    elif request.get("stage_actions_sha256") != _digest(actions[start : end + 1]):
        errors.append("PARTIAL_PLAN_APPROVAL")
    try:
        current = _time(now)
        issued, expires = _time(request["issued_at"]), _time(request["expires_at"])
        if current < issued:
            errors.append("AUTHORIZATION_NOT_YET_VALID")
        if current > expires:
            errors.append("AUTHORIZATION_EXPIRED")
    except (TypeError, ValueError, KeyError):
        errors.append("AUTHORIZATION_TIME_INVALID")
    revoked_signatures = {
        row.get("signature_sha256")
        for row in revocations or []
        if row.get("request_sha256") == request.get("request_sha256")
    }
    reviewer_ids, signatures, domains = [], [], []
    for approval in approvals:
        reviewer_id = approval.get("reviewer_id")
        reviewer = registry.get(reviewer_id)
        if not isinstance(reviewer, dict) or reviewer.get("status") != "AUTHORIZED":
            errors.append("REVIEWER_UNAUTHORIZED")
            continue
        if reviewer_id == request.get("requester_id"):
            errors.append("SELF_APPROVAL_PROHIBITED")
        body = {key: value for key, value in approval.items() if key != "signature_sha256"}
        if approval.get("signature_sha256") != _digest({"key": reviewer["key"], "approval": body}):
            errors.append("APPROVAL_SIGNATURE_INVALID")
        if (
            approval.get("request_sha256") != request.get("request_sha256")
            or approval.get("stage_start") != start
            or approval.get("stage_end") != end
            or approval.get("nonce") != request.get("nonce")
        ):
            errors.append("APPROVAL_SCOPE_MISMATCH")
        try:
            approved_at = _time(approval["approved_at"])
            if approved_at < issued or approved_at > expires:
                errors.append("APPROVAL_STALE")
            if approved_at > current:
                errors.append("APPROVAL_FROM_FUTURE")
        except (TypeError, ValueError, KeyError, UnboundLocalError):
            errors.append("APPROVAL_TIME_INVALID")
        if approval.get("signature_sha256") in revoked_signatures:
            errors.append("APPROVAL_REVOKED")
        reviewer_ids.append(reviewer_id)
        signatures.append(approval.get("signature_sha256"))
        domains.append(reviewer.get("domain"))
    if len(set(reviewer_ids)) < 2 or len(approvals) < 2:
        errors.append("TWO_PERSON_APPROVAL_MISSING")
    if len(signatures) != len(set(signatures)):
        errors.append("APPROVAL_SIGNATURE_REUSED")
    if len(set(domains)) < 2:
        errors.append("REVIEWER_INDEPENDENCE_MISSING")
    if context.get("rolled_back") or context.get("failed_stage") is not None:
        errors.append("REAUTHORIZATION_REQUIRED")
    authorization_id = _digest(
        {"request_sha256": request.get("request_sha256"), "signatures": sorted(signatures)}
    )
    if authorization_id in (used_authorization_ids or set()):
        errors.append("AUTHORIZATION_REPLAYED")
    grant = None
    if not errors:
        grant = {
            "authorization_id": authorization_id,
            "request_sha256": request["request_sha256"],
            "stage_start": start,
            "stage_end": end,
            "capability": "OFFLINE_SIMULATION_ONLY",
            "expires_at": request["expires_at"],
            "safety": _safety(),
        }
        grant["grant_sha256"] = _digest(grant)
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "authorization_id": authorization_id,
        "grant": grant,
        "safety": _safety(),
    }
    result["verification_sha256"] = _digest(result)
    return result


def consume_simulation_grant(
    grant: dict[str, object], *, stage: int, used_authorization_ids: set[str]
) -> dict[str, object]:
    errors = []
    if grant.get("capability") != "OFFLINE_SIMULATION_ONLY":
        errors.append("GRANT_CAPABILITY_INVALID")
    if not grant.get("stage_start") <= stage <= grant.get("stage_end"):
        errors.append("STAGE_NOT_AUTHORIZED")
    if grant.get("authorization_id") in used_authorization_ids:
        errors.append("AUTHORIZATION_REPLAYED")
    result = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "simulated_stage": stage if not errors else None,
        "next_used_authorization_ids": sorted(
            used_authorization_ids | ({grant["authorization_id"]} if not errors else set())
        ),
        "safety": _safety(),
    }
    result["consumption_sha256"] = _digest(result)
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
