from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

RELOAD_SCHEMA_VERSION = "phase4ka-safe-configuration-reload-v1"
ALLOWLISTED_FIELDS = frozenset(
    {"observation_interval_seconds", "alert_rate_limit", "diagnostic_record_limit"}
)
ReloadStatus = Literal["READY", "NO_CHANGE", "DENIED", "INCOMPLETE", "TAMPERED"]


class SafeConfigurationReloadError(ValueError):
    """Stable fail-closed safe configuration reload error."""


@dataclass(frozen=True)
class ConfigurationReloadRequest:
    current_configuration_hash: str
    candidate_configuration_hash: str
    signature_decision_hash: str
    rollback_snapshot_hash: str
    signature_status: str
    changed_fields: tuple[str, ...]
    supervisor_healthy: bool
    recovery_action_pending: bool
    atomic_swap_proven: bool
    rollback_proven: bool
    complete: bool
    request_hash: str


@dataclass(frozen=True)
class ConfigurationReloadDecision:
    status: ReloadStatus
    reasons: tuple[str, ...]
    request_hash: str
    current_configuration_hash: str
    candidate_configuration_hash: str
    canonical_changed_fields: tuple[str, ...]
    reload_plan_hash: str
    decision_hash: str
    read_only: bool = True
    reload_plan_ready: bool = False
    reload_application_authorized: bool = False
    task_activation_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_configuration_reload_request(**fields: Any) -> ConfigurationReloadRequest:
    normalized = dict(fields)
    if isinstance(normalized.get("changed_fields"), list):
        normalized["changed_fields"] = tuple(normalized["changed_fields"])
    _validate_fields(normalized)
    normalized["changed_fields"] = tuple(sorted(normalized["changed_fields"]))
    unsigned = {**normalized, "changed_fields": list(normalized["changed_fields"])}
    return ConfigurationReloadRequest(**normalized, request_hash=_hash(unsigned))


def evaluate_safe_configuration_reload(
    request: Any,
) -> ConfigurationReloadDecision:
    item = _validated_request(request)
    fields = set(item.changed_fields)
    duplicates = len(fields) != len(item.changed_fields)
    forbidden = sorted(fields - ALLOWLISTED_FIELDS)
    same_hash = item.current_configuration_hash == item.candidate_configuration_hash
    if duplicates or forbidden or (same_hash and fields) or (not same_hash and not fields):
        status: ReloadStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("CONFIG_RELOAD_FIELD_DUPLICATE")
        reasons.extend(f"CONFIG_RELOAD_FIELD_FORBIDDEN:{field}" for field in forbidden)
        if (same_hash and fields) or (not same_hash and not fields):
            reasons.append("CONFIG_RELOAD_CHANGESET_HASH_CONTRADICTION")
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["CONFIG_RELOAD_REQUEST_INCOMPLETE"]
    elif same_hash:
        status = "NO_CHANGE"
        reasons = []
    else:
        reasons = []
        if item.signature_status != "VALID":
            reasons.append("CONFIG_RELOAD_SIGNATURE_INVALID")
        if not item.supervisor_healthy:
            reasons.append("CONFIG_RELOAD_SUPERVISOR_UNHEALTHY")
        if item.recovery_action_pending:
            reasons.append("CONFIG_RELOAD_RECOVERY_PENDING")
        if not item.atomic_swap_proven:
            reasons.append("CONFIG_RELOAD_ATOMIC_SWAP_UNPROVEN")
        if not item.rollback_proven:
            reasons.append("CONFIG_RELOAD_ROLLBACK_UNPROVEN")
        status = "DENIED" if reasons else "READY"
    ready = status == "READY"
    plan = {
        "current_configuration_hash": item.current_configuration_hash,
        "candidate_configuration_hash": item.candidate_configuration_hash,
        "signature_decision_hash": item.signature_decision_hash,
        "rollback_snapshot_hash": item.rollback_snapshot_hash,
        "changed_fields": list(item.changed_fields),
        "atomic_swap_required": True,
        "rollback_required": True,
    }
    unsigned = {
        "schema_version": RELOAD_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "request_hash": item.request_hash,
        "current_configuration_hash": item.current_configuration_hash,
        "candidate_configuration_hash": item.candidate_configuration_hash,
        "canonical_changed_fields": list(item.changed_fields),
        "reload_plan_hash": _hash(plan),
        "read_only": True,
        "reload_plan_ready": ready,
        "reload_application_authorized": False,
        "task_activation_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return ConfigurationReloadDecision(
        status=status,
        reasons=tuple(reasons),
        request_hash=item.request_hash,
        current_configuration_hash=item.current_configuration_hash,
        candidate_configuration_hash=item.candidate_configuration_hash,
        canonical_changed_fields=item.changed_fields,
        reload_plan_hash=unsigned["reload_plan_hash"],
        decision_hash=_hash(unsigned),
        reload_plan_ready=ready,
    )


def validate_configuration_reload_decision(value: Any) -> None:
    if not isinstance(value, ConfigurationReloadDecision):
        raise SafeConfigurationReloadError("CONFIG_RELOAD_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.reload_plan_ready != (value.status == "READY")
        or any(
            (
                value.reload_application_authorized,
                value.task_activation_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise SafeConfigurationReloadError("CONFIG_RELOAD_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = RELOAD_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["canonical_changed_fields"] = list(unsigned["canonical_changed_fields"])
    if value.decision_hash != _hash(unsigned):
        raise SafeConfigurationReloadError("CONFIG_RELOAD_DECISION_HASH_MISMATCH")


def _validated_request(value: Any) -> ConfigurationReloadRequest:
    if not isinstance(value, ConfigurationReloadRequest):
        raise SafeConfigurationReloadError("CONFIG_RELOAD_REQUEST_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("request_hash")
    unsigned["changed_fields"] = tuple(unsigned["changed_fields"])
    _validate_fields(unsigned)
    unsigned["changed_fields"] = list(unsigned["changed_fields"])
    if supplied != _hash(unsigned):
        raise SafeConfigurationReloadError("CONFIG_RELOAD_REQUEST_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "current_configuration_hash",
        "candidate_configuration_hash",
        "signature_decision_hash",
        "rollback_snapshot_hash",
        "signature_status",
        "changed_fields",
        "supervisor_healthy",
        "recovery_action_pending",
        "atomic_swap_proven",
        "rollback_proven",
        "complete",
    }
    if set(fields) != required:
        raise SafeConfigurationReloadError("CONFIG_RELOAD_REQUEST_FIELD_INVALID")
    for key in (
        "current_configuration_hash",
        "candidate_configuration_hash",
        "signature_decision_hash",
        "rollback_snapshot_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SafeConfigurationReloadError("CONFIG_RELOAD_REQUEST_FIELD_INVALID")
    if (
        not isinstance(fields["signature_status"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", fields["signature_status"]) is None
        or not isinstance(fields["changed_fields"], tuple)
        or len(fields["changed_fields"]) > 32
        or any(
            not isinstance(field, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", field) is None
            for field in fields["changed_fields"]
        )
    ):
        raise SafeConfigurationReloadError("CONFIG_RELOAD_REQUEST_FIELD_INVALID")
    for key in (
        "supervisor_healthy",
        "recovery_action_pending",
        "atomic_swap_proven",
        "rollback_proven",
        "complete",
    ):
        if not isinstance(fields[key], bool):
            raise SafeConfigurationReloadError("CONFIG_RELOAD_REQUEST_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
