"""Validate quorum, revocation, and trust-policy rotation evidence offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime

POLICY_SCHEMA = "phase4ly.trust-policy.v1"
RESULT_SCHEMA = "phase4ly.trust-policy-rotation-validation.v1"
COMPOSITION_SCHEMA = "phase4lx.independent-attestation-composition.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
ROLES = {"BUILDER", "SCANNER", "RECOVERY"}
MAX_OVERLAP_SECONDS = 3_600
MAX_EVIDENCE_SECONDS = 86_400
AUTHORITY_FIELDS = {"authority_id", "role", "authority_sha256", "status"}
POLICY_FIELDS = {
    "schema",
    "epoch",
    "predecessor_policy_sha256",
    "composition_sha256",
    "effective_at",
    "overlap_until",
    "mode",
    "quorum",
    "authorities",
    "capabilities",
    "policy_sha256",
}
QUORUM_FIELDS = {"required_roles", "threshold"}
CAPABILITY_FIELDS = {
    "runtime_control",
    "key_access",
    "network_access",
    "service_control",
    "trading",
}
APPROVAL_FIELDS = {
    "authority_id",
    "role",
    "current_policy_sha256",
    "candidate_policy_sha256",
    "issued_at",
    "expires_at",
    "replay_identity_sha256",
    "self_approval",
    "approval_sha256",
}
REVOCATION_FIELDS = {
    "target_authority_id",
    "target_authority_sha256",
    "reason",
    "current_policy_sha256",
    "candidate_policy_sha256",
    "issued_at",
    "expires_at",
    "signer_authority_ids",
    "replay_identity_sha256",
    "revocation_sha256",
}


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


def make_policy(**fields: object) -> dict[str, object]:
    result = {"schema": POLICY_SCHEMA, **fields}
    result["policy_sha256"] = _digest(result)
    return result


def make_approval(**fields: object) -> dict[str, object]:
    result = dict(fields)
    result["approval_sha256"] = _digest(result)
    return result


def make_revocation(**fields: object) -> dict[str, object]:
    result = dict(fields)
    result["revocation_sha256"] = _digest(result)
    return result


def _policy_errors(policy: object, label: str) -> tuple[list[str], dict[str, dict[str, object]]]:
    errors: list[str] = []
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
        return [f"{label}:FIELD_SET_INVALID"], {}
    body = {key: value for key, value in policy.items() if key != "policy_sha256"}
    if policy.get("policy_sha256") != _digest(body):
        errors.append(f"{label}:HASH_MISMATCH")
    if policy.get("schema") != POLICY_SCHEMA:
        errors.append(f"{label}:SCHEMA_INVALID")
    if policy.get("mode") != "FIXTURE_ONLY":
        errors.append(f"{label}:MODE_INVALID")
    epoch = policy.get("epoch")
    if type(epoch) is not int or epoch < 1:
        errors.append(f"{label}:EPOCH_INVALID")
    elif epoch == 1 and policy.get("predecessor_policy_sha256") is not None:
        errors.append(f"{label}:GENESIS_PREDECESSOR_INVALID")
    elif epoch > 1 and HEX64.fullmatch(str(policy.get("predecessor_policy_sha256"))) is None:
        errors.append(f"{label}:PREDECESSOR_FORMAT_INVALID")
    quorum = policy.get("quorum")
    if not isinstance(quorum, dict) or set(quorum) != QUORUM_FIELDS:
        errors.append(f"{label}:QUORUM_FIELD_SET_INVALID")
    elif quorum.get("required_roles") != sorted(ROLES) or quorum.get("threshold") != 2:
        errors.append(f"{label}:QUORUM_REDUCTION_OR_CHANGE")
    capabilities = policy.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != CAPABILITY_FIELDS:
        errors.append(f"{label}:CAPABILITY_FIELD_SET_INVALID")
    elif any(value is not False for value in capabilities.values()):
        errors.append(f"{label}:AUTHORITY_BROADENING")
    authorities = policy.get("authorities")
    by_id: dict[str, dict[str, object]] = {}
    roles: set[str] = set()
    digests: set[str] = set()
    if not isinstance(authorities, list) or len(authorities) != 3:
        errors.append(f"{label}:EXACT_AUTHORITY_SET_REQUIRED")
        authorities = []
    elif [authority.get("role") for authority in authorities if isinstance(authority, dict)] != [
        "BUILDER",
        "SCANNER",
        "RECOVERY",
    ]:
        errors.append(f"{label}:AUTHORITY_ORDER_INVALID")
    for index, authority in enumerate(authorities):
        if not isinstance(authority, dict) or set(authority) != AUTHORITY_FIELDS:
            errors.append(f"{label}:AUTHORITY_{index}:FIELD_SET_INVALID")
            continue
        authority_id = authority.get("authority_id")
        role = authority.get("role")
        digest = authority.get("authority_sha256")
        if not isinstance(authority_id, str) or not authority_id:
            errors.append(f"{label}:AUTHORITY_{index}:ID_INVALID")
        elif authority_id in by_id:
            errors.append(f"{label}:AUTHORITY_{index}:DUPLICATE_ID")
        else:
            by_id[authority_id] = authority
        if role not in ROLES or role in roles:
            errors.append(f"{label}:AUTHORITY_{index}:ROLE_INVALID_OR_DUPLICATE")
        else:
            roles.add(str(role))
        if HEX64.fullmatch(str(digest)) is None or digest in digests:
            errors.append(f"{label}:AUTHORITY_{index}:DIGEST_INVALID_OR_DUPLICATE")
        else:
            digests.add(str(digest))
        if authority.get("status") != "ACTIVE":
            errors.append(f"{label}:AUTHORITY_{index}:STATUS_INVALID")
    if roles != ROLES:
        errors.append(f"{label}:ROLE_COVERAGE_INVALID")
    return errors, by_id


def validate_rotation(
    composition: object,
    current: object,
    candidate: object,
    approvals: object,
    revocations: object,
    replay_set: object,
    *,
    evaluated_at: str,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(composition, dict) or composition.get("schema") != COMPOSITION_SCHEMA:
        errors.append("COMPOSITION_INVALID")
        composition = {}
    else:
        body = {key: value for key, value in composition.items() if key != "composition_sha256"}
        if composition.get("composition_sha256") != _digest(body):
            errors.append("COMPOSITION_HASH_MISMATCH")
        if composition.get("fixture_readiness") != "PASS":
            errors.append("COMPOSITION_FIXTURE_NOT_PASSING")
    current_errors, current_by_id = _policy_errors(current, "CURRENT")
    candidate_errors, candidate_by_id = _policy_errors(candidate, "CANDIDATE")
    errors.extend(current_errors + candidate_errors)
    current = current if isinstance(current, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    if current.get("composition_sha256") != composition.get("composition_sha256"):
        errors.append("CURRENT_COMPOSITION_BINDING_MISMATCH")
    if candidate.get("composition_sha256") != composition.get("composition_sha256"):
        errors.append("CANDIDATE_COMPOSITION_BINDING_MISMATCH")
    current_epoch = current.get("epoch")
    candidate_epoch = candidate.get("epoch")
    if type(current_epoch) is int and candidate_epoch != current_epoch + 1:
        errors.append("EPOCH_ROLLBACK_SKIP_OR_REUSE")
    if candidate.get("predecessor_policy_sha256") != current.get("policy_sha256"):
        errors.append("PREDECESSOR_BINDING_MISMATCH")
    effective = _time(candidate.get("effective_at"))
    overlap = _time(candidate.get("overlap_until"))
    current_effective = _time(current.get("effective_at"))
    now = _time(evaluated_at)
    if effective is None or overlap is None or current_effective is None or now is None:
        errors.append("POLICY_TIME_INVALID")
    elif (
        effective < current_effective
        or overlap < effective
        or (overlap - effective).total_seconds() > MAX_OVERLAP_SECONDS
        or now > overlap
    ):
        errors.append("POLICY_TIME_OR_OVERLAP_INVALID")
    if not isinstance(replay_set, list) or not all(isinstance(item, str) for item in replay_set):
        errors.append("REPLAY_SET_INVALID")
        replay_set = []
    seen_replay = set(replay_set)
    if not isinstance(approvals, list) or len(approvals) < 2:
        errors.append("APPROVAL_QUORUM_REQUIRED")
        approvals = []
    approval_roles: set[str] = set()
    approval_authorities: set[str] = set()
    approval_records: list[dict[str, object]] = []
    added_ids = set(candidate_by_id) - set(current_by_id)
    removed_ids = set(current_by_id) - set(candidate_by_id)
    for index, approval in enumerate(approvals):
        item_errors: list[str] = []
        if not isinstance(approval, dict) or set(approval) != APPROVAL_FIELDS:
            item_errors.append("FIELD_SET_INVALID")
            approval = {}
        body = {key: value for key, value in approval.items() if key != "approval_sha256"}
        if approval.get("approval_sha256") != _digest(body):
            item_errors.append("HASH_MISMATCH")
        authority_id = approval.get("authority_id")
        authority = current_by_id.get(str(authority_id))
        if authority is None:
            item_errors.append("APPROVER_NOT_CURRENT")
        elif authority.get("role") != approval.get("role"):
            item_errors.append("APPROVER_ROLE_MISMATCH")
        if authority_id in added_ids or approval.get("self_approval") is not False:
            item_errors.append("SELF_APPROVAL")
        if authority_id in removed_ids:
            item_errors.append("REMOVED_AUTHORITY_CANNOT_APPROVE")
        if authority_id in approval_authorities:
            item_errors.append("DUPLICATE_APPROVER")
        elif isinstance(authority_id, str):
            approval_authorities.add(authority_id)
        role = approval.get("role")
        if isinstance(role, str):
            approval_roles.add(role)
        for field, expected in (
            ("current_policy_sha256", current.get("policy_sha256")),
            ("candidate_policy_sha256", candidate.get("policy_sha256")),
        ):
            if approval.get(field) != expected:
                item_errors.append(f"BINDING_MISMATCH:{field}")
        issued = _time(approval.get("issued_at"))
        expires = _time(approval.get("expires_at"))
        if issued is None or expires is None or now is None:
            item_errors.append("TIME_INVALID")
        elif (
            expires <= issued
            or (expires - issued).total_seconds() > MAX_EVIDENCE_SECONDS
            or not issued <= now < expires
        ):
            item_errors.append("STALE_FUTURE_OR_OVERLONG")
        replay = approval.get("replay_identity_sha256")
        if HEX64.fullmatch(str(replay)) is None or replay in seen_replay:
            item_errors.append("REPLAY_INVALID_OR_DUPLICATE")
        else:
            seen_replay.add(str(replay))
        errors.extend(f"APPROVAL_{index}:{error}" for error in item_errors)
        approval_records.append(
            {
                "authority_id": authority_id,
                "role": role,
                "approval_sha256": approval.get("approval_sha256"),
                "verdict": "PASS" if not item_errors else "REFUSE",
            }
        )
    if len(approval_authorities) < 2 or len(approval_roles) < 2:
        errors.append("DISTINCT_ROLE_QUORUM_NOT_MET")
    if not isinstance(revocations, list):
        errors.append("REVOCATIONS_NOT_A_LIST")
        revocations = []
    revoked_targets: set[str] = set()
    revocation_records: list[dict[str, object]] = []
    for index, revocation in enumerate(revocations):
        item_errors: list[str] = []
        if not isinstance(revocation, dict) or set(revocation) != REVOCATION_FIELDS:
            item_errors.append("FIELD_SET_INVALID")
            revocation = {}
        body = {key: value for key, value in revocation.items() if key != "revocation_sha256"}
        if revocation.get("revocation_sha256") != _digest(body):
            item_errors.append("HASH_MISMATCH")
        target = revocation.get("target_authority_id")
        target_record = current_by_id.get(str(target))
        if target not in removed_ids or target_record is None:
            item_errors.append("TARGET_NOT_REMOVED_CURRENT_AUTHORITY")
        elif revocation.get("target_authority_sha256") != target_record.get("authority_sha256"):
            item_errors.append("TARGET_DIGEST_MISMATCH")
        if target in revoked_targets:
            item_errors.append("DUPLICATE_TARGET")
        elif isinstance(target, str):
            revoked_targets.add(target)
        if revocation.get("reason") not in {"COMPROMISE", "EXPIRY", "ADMIN_ROTATION"}:
            item_errors.append("REASON_INVALID")
        signers = revocation.get("signer_authority_ids")
        allowed_signers = set(current_by_id) - {target}
        if not isinstance(signers, list) or len(signers) != 2 or set(signers) != allowed_signers:
            item_errors.append("EMERGENCY_SIGNER_QUORUM_INVALID")
        for field, expected in (
            ("current_policy_sha256", current.get("policy_sha256")),
            ("candidate_policy_sha256", candidate.get("policy_sha256")),
        ):
            if revocation.get(field) != expected:
                item_errors.append(f"BINDING_MISMATCH:{field}")
        issued = _time(revocation.get("issued_at"))
        expires = _time(revocation.get("expires_at"))
        if issued is None or expires is None or now is None:
            item_errors.append("TIME_INVALID")
        elif (
            expires <= issued
            or (expires - issued).total_seconds() > MAX_EVIDENCE_SECONDS
            or not issued <= now < expires
        ):
            item_errors.append("STALE_FUTURE_OR_OVERLONG")
        replay = revocation.get("replay_identity_sha256")
        if HEX64.fullmatch(str(replay)) is None or replay in seen_replay:
            item_errors.append("REPLAY_INVALID_OR_DUPLICATE")
        else:
            seen_replay.add(str(replay))
        errors.extend(f"REVOCATION_{index}:{error}" for error in item_errors)
        revocation_records.append(
            {
                "target_authority_id": target,
                "revocation_sha256": revocation.get("revocation_sha256"),
                "verdict": "PASS" if not item_errors else "REFUSE",
            }
        )
    if revoked_targets != removed_ids:
        errors.append("REMOVAL_REVOCATION_SET_MISMATCH")
    result: dict[str, object] = {
        "schema": RESULT_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "fixture_rotation_readiness": "PASS" if not errors else "REFUSE",
        "production_readiness": "REFUSE",
        "production_reasons": ["NO_GENUINE_PRODUCTION_AUTHORITY_SET"],
        "errors": sorted(set(errors)),
        "current_policy_sha256": current.get("policy_sha256"),
        "candidate_policy_sha256": candidate.get("policy_sha256"),
        "composition_sha256": composition.get("composition_sha256"),
        "approvals": sorted(approval_records, key=lambda row: str(row["role"])),
        "revocations": sorted(revocation_records, key=lambda row: str(row["target_authority_id"])),
        "safety": {
            "validation_only": True,
            "policy_activation": False,
            "revocation_publication": False,
            "key_access": False,
            "network_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["rotation_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("composition", "current", "candidate", "approvals", "revocations", "replay_set"):
        parser.add_argument(name)
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args()
    values = []
    for name in ("composition", "current", "candidate", "approvals", "revocations", "replay_set"):
        with open(getattr(args, name), encoding="utf-8") as stream:
            values.append(json.load(stream))
    result = validate_rotation(*values, evaluated_at=args.evaluated_at)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
