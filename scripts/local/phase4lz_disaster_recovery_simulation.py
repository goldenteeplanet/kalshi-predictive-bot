"""Validate trust-policy disaster-recovery and break-glass simulations."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime

PLAN_SCHEMA = "phase4lz.recovery-plan.v1"
INCIDENT_SCHEMA = "phase4lz.recovery-incident.v1"
RESULT_SCHEMA = "phase4lz.disaster-recovery-simulation.v1"
ROTATION_SCHEMA = "phase4ly.trust-policy-rotation-validation.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
ROLES = ["BUILDER", "SCANNER", "RECOVERY"]
FAILURE_MODES = {
    "AUTHORITY_LOSS",
    "AUTHORITY_COMPROMISE",
    "POLICY_STORE_CORRUPTION",
    "CLOCK_UNCERTAINTY",
    "REPLAY_CACHE_LOSS",
    "RECOVERY_MEDIA_UNAVAILABLE",
}
REQUIRED_ACTIONS = [
    "REVOKE_COMPROMISED",
    "ROTATE_POLICY",
    "REBUILD_REPLAY_SET",
    "RESTORE_QUORUM",
    "CAPTURE_AFTER_ACTION",
]
PLAN_FIELDS = {
    "schema",
    "plan_id_sha256",
    "rotation_sha256",
    "current_recovery_epoch",
    "authority_ids",
    "custodians",
    "recovery_media_sha256",
    "max_emergency_seconds",
    "required_roles",
    "required_actions",
    "capabilities",
    "plan_sha256",
}
INCIDENT_FIELDS = {
    "schema",
    "incident_id_sha256",
    "plan_sha256",
    "opened_at",
    "emergency_expires_at",
    "failure_modes",
    "affected_authority_ids",
    "recovery_epoch",
    "custodian_approvals",
    "recovery_media",
    "clock_evidence",
    "revoked_authority_ids",
    "restored_authorities",
    "rotation_sha256",
    "replay_reconstruction",
    "after_action",
    "capabilities",
    "incident_sha256",
}
CAPABILITY_FIELDS = {
    "policy_activation",
    "material_access",
    "runtime_control",
    "network_access",
    "service_control",
    "trading",
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


def make_plan(**fields: object) -> dict[str, object]:
    result = {"schema": PLAN_SCHEMA, **fields}
    result["plan_sha256"] = _digest(result)
    return result


def make_incident(**fields: object) -> dict[str, object]:
    result = {"schema": INCIDENT_SCHEMA, **fields}
    result["incident_sha256"] = _digest(result)
    return result


def simulate_recovery(
    rotation: object,
    plan: object,
    incident: object,
    prior_incident_ids: object,
    *,
    evaluated_at: str,
) -> dict[str, object]:
    errors: list[str] = []
    now = _time(evaluated_at)
    if now is None:
        errors.append("EVALUATION_TIME_INVALID")
    if not isinstance(rotation, dict) or rotation.get("schema") != ROTATION_SCHEMA:
        errors.append("ROTATION_INVALID")
        rotation = {}
    else:
        body = {key: value for key, value in rotation.items() if key != "rotation_sha256"}
        if rotation.get("rotation_sha256") != _digest(body):
            errors.append("ROTATION_HASH_MISMATCH")
        if rotation.get("fixture_rotation_readiness") != "PASS":
            errors.append("ROTATION_FIXTURE_NOT_PASSING")
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        errors.append("PLAN_FIELD_SET_INVALID")
        plan = {}
    else:
        body = {key: value for key, value in plan.items() if key != "plan_sha256"}
        if plan.get("plan_sha256") != _digest(body):
            errors.append("PLAN_HASH_MISMATCH")
    if plan.get("schema") != PLAN_SCHEMA:
        errors.append("PLAN_SCHEMA_INVALID")
    if plan.get("rotation_sha256") != rotation.get("rotation_sha256"):
        errors.append("PLAN_ROTATION_BINDING_MISMATCH")
    if HEX64.fullmatch(str(plan.get("plan_id_sha256"))) is None:
        errors.append("PLAN_ID_INVALID")
    epoch = plan.get("current_recovery_epoch")
    if type(epoch) is not int or epoch < 0:
        errors.append("PLAN_EPOCH_INVALID")
    authority_ids = plan.get("authority_ids")
    if (
        not isinstance(authority_ids, list)
        or len(authority_ids) != 3
        or len(set(authority_ids)) != 3
        or not all(isinstance(item, str) and item for item in authority_ids)
    ):
        errors.append("PLAN_AUTHORITY_SET_INVALID")
        authority_ids = []
    custodians = plan.get("custodians")
    if (
        not isinstance(custodians, list)
        or len(custodians) != 2
        or len({item.get("custodian_id") for item in custodians if isinstance(item, dict)}) != 2
        or any(
            not isinstance(item, dict)
            or set(item) != {"custodian_id", "custodian_sha256"}
            or HEX64.fullmatch(str(item.get("custodian_sha256"))) is None
            for item in custodians
        )
    ):
        errors.append("TWO_PERSON_CUSTODY_INVALID")
        custodians = []
    if HEX64.fullmatch(str(plan.get("recovery_media_sha256"))) is None:
        errors.append("RECOVERY_MEDIA_PIN_INVALID")
    max_seconds = plan.get("max_emergency_seconds")
    if type(max_seconds) is not int or not 60 <= max_seconds <= 3_600:
        errors.append("EMERGENCY_BOUND_INVALID")
    if plan.get("required_roles") != ROLES or plan.get("required_actions") != REQUIRED_ACTIONS:
        errors.append("RECOVERY_REQUIREMENTS_INVALID")
    capabilities = plan.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != CAPABILITY_FIELDS:
        errors.append("PLAN_CAPABILITY_FIELD_SET_INVALID")
    elif any(value is not False for value in capabilities.values()):
        errors.append("PLAN_AUTHORITY_BROADENING")
    if not isinstance(incident, dict) or set(incident) != INCIDENT_FIELDS:
        errors.append("INCIDENT_FIELD_SET_INVALID")
        incident = {}
    else:
        body = {key: value for key, value in incident.items() if key != "incident_sha256"}
        if incident.get("incident_sha256") != _digest(body):
            errors.append("INCIDENT_HASH_MISMATCH")
    if incident.get("schema") != INCIDENT_SCHEMA:
        errors.append("INCIDENT_SCHEMA_INVALID")
    incident_id = incident.get("incident_id_sha256")
    if HEX64.fullmatch(str(incident_id)) is None:
        errors.append("INCIDENT_ID_INVALID")
    if not isinstance(prior_incident_ids, list) or not all(
        isinstance(item, str) for item in prior_incident_ids
    ):
        errors.append("PRIOR_INCIDENT_SET_INVALID")
        prior_incident_ids = []
    if incident_id in prior_incident_ids:
        errors.append("INCIDENT_ID_REUSED")
    if incident.get("plan_sha256") != plan.get("plan_sha256"):
        errors.append("INCIDENT_PLAN_BINDING_MISMATCH")
    opened = _time(incident.get("opened_at"))
    emergency_expires = _time(incident.get("emergency_expires_at"))
    if opened is None or emergency_expires is None or now is None or type(max_seconds) is not int:
        errors.append("INCIDENT_TIME_INVALID")
    elif (
        emergency_expires <= opened
        or (emergency_expires - opened).total_seconds() > max_seconds
        or not opened <= now < emergency_expires
    ):
        errors.append("EMERGENCY_WINDOW_INVALID")
    modes = incident.get("failure_modes")
    if (
        not isinstance(modes, list)
        or not modes
        or len(modes) != len(set(modes))
        or not set(modes) <= FAILURE_MODES
    ):
        errors.append("FAILURE_MODES_INVALID")
    affected = incident.get("affected_authority_ids")
    if (
        not isinstance(affected, list)
        or not 1 <= len(affected) <= 2
        or len(affected) != len(set(affected))
        or not set(affected) <= set(authority_ids)
    ):
        errors.append("AFFECTED_AUTHORITY_SET_INVALID")
        affected = []
    if incident.get("recovery_epoch") != (epoch + 1 if type(epoch) is int else None):
        errors.append("RECOVERY_EPOCH_NOT_MONOTONIC")
    approvals = incident.get("custodian_approvals")
    expected_custodians = {item.get("custodian_id") for item in custodians}
    if (
        not isinstance(approvals, list)
        or len(approvals) != 2
        or {item.get("custodian_id") for item in approvals if isinstance(item, dict)}
        != expected_custodians
        or any(
            not isinstance(item, dict)
            or set(item) != {"custodian_id", "plan_sha256", "incident_id_sha256", "approval_sha256"}
            or item.get("plan_sha256") != plan.get("plan_sha256")
            or item.get("incident_id_sha256") != incident_id
            or item.get("approval_sha256")
            != _digest({key: value for key, value in item.items() if key != "approval_sha256"})
            for item in approvals
        )
    ):
        errors.append("CUSTODIAN_APPROVALS_INVALID")
    media = incident.get("recovery_media")
    if (
        not isinstance(media, dict)
        or set(media) != {"available", "media_sha256", "material_accessed"}
        or media.get("available") is not True
        or media.get("material_accessed") is not False
        or media.get("media_sha256") != plan.get("recovery_media_sha256")
    ):
        errors.append("RECOVERY_MEDIA_UNAVAILABLE_OR_UNPINNED")
    clock = incident.get("clock_evidence")
    if (
        not isinstance(clock, dict)
        or set(clock) != {"status", "observed_at", "source_count", "evidence_sha256"}
        or clock.get("status") != "UNAMBIGUOUS"
        or _time(clock.get("observed_at")) is None
        or type(clock.get("source_count")) is not int
        or clock.get("source_count") < 2
        or HEX64.fullmatch(str(clock.get("evidence_sha256"))) is None
    ):
        errors.append("CLOCK_EVIDENCE_AMBIGUOUS_OR_INVALID")
    revoked = incident.get("revoked_authority_ids")
    if not isinstance(revoked, list) or set(revoked) != set(affected):
        errors.append("REVOCATION_SET_INCOMPLETE")
    restored = incident.get("restored_authorities")
    if (
        not isinstance(restored, list)
        or len(restored) != 3
        or [item.get("role") for item in restored if isinstance(item, dict)] != ROLES
        or len({item.get("authority_id") for item in restored if isinstance(item, dict)}) != 3
        or any(
            not isinstance(item, dict)
            or set(item) != {"authority_id", "role", "authority_sha256", "status"}
            or item.get("status") != "ACTIVE"
            or HEX64.fullmatch(str(item.get("authority_sha256"))) is None
            for item in restored
        )
    ):
        errors.append("FULL_QUORUM_RESTORATION_INVALID")
    elif any(item.get("authority_id") in set(affected) for item in restored):
        errors.append("AFFECTED_AUTHORITY_NOT_REPLACED")
    if incident.get("rotation_sha256") != rotation.get("rotation_sha256"):
        errors.append("MANDATORY_ROTATION_BINDING_MISMATCH")
    replay = incident.get("replay_reconstruction")
    if (
        not isinstance(replay, dict)
        or set(replay) != {"complete", "prior_head_sha256", "new_head_sha256", "evidence_sha256"}
        or replay.get("complete") is not True
        or any(
            HEX64.fullmatch(str(replay.get(field))) is None
            for field in (
                "prior_head_sha256",
                "new_head_sha256",
                "evidence_sha256",
            )
        )
        or replay.get("prior_head_sha256") == replay.get("new_head_sha256")
    ):
        errors.append("REPLAY_SET_RECONSTRUCTION_INVALID")
    after_action = incident.get("after_action")
    custodian_ids = expected_custodians
    if (
        not isinstance(after_action, dict)
        or set(after_action) != {"completed", "independent_authority_id", "evidence_sha256"}
        or after_action.get("completed") is not True
        or after_action.get("independent_authority_id") in custodian_ids
        or not isinstance(after_action.get("independent_authority_id"), str)
        or HEX64.fullmatch(str(after_action.get("evidence_sha256"))) is None
    ):
        errors.append("AFTER_ACTION_EVIDENCE_INVALID")
    incident_capabilities = incident.get("capabilities")
    if (
        not isinstance(incident_capabilities, dict)
        or set(incident_capabilities) != CAPABILITY_FIELDS
    ):
        errors.append("INCIDENT_CAPABILITY_FIELD_SET_INVALID")
    elif any(value is not False for value in incident_capabilities.values()):
        errors.append("INCIDENT_AUTHORITY_BROADENING")
    result: dict[str, object] = {
        "schema": RESULT_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "fixture_recovery_readiness": "PASS" if not errors else "REFUSE",
        "production_readiness": "REFUSE",
        "production_reasons": ["NO_PRODUCTION_RECOVERY_AUTHORITY_OR_MEDIA"],
        "errors": sorted(set(errors)),
        "rotation_sha256": rotation.get("rotation_sha256"),
        "plan_sha256": plan.get("plan_sha256"),
        "incident_sha256": incident.get("incident_sha256"),
        "incident_id_sha256": incident_id,
        "recovery_epoch": incident.get("recovery_epoch"),
        "safety": {
            "simulation_only": True,
            "policy_activation": False,
            "material_access": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["simulation_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("rotation", "plan", "incident", "prior_incident_ids"):
        parser.add_argument(name)
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args()
    values = []
    for name in ("rotation", "plan", "incident", "prior_incident_ids"):
        with open(getattr(args, name), encoding="utf-8") as stream:
            values.append(json.load(stream))
    result = simulate_recovery(*values, evaluated_at=args.evaluated_at)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
