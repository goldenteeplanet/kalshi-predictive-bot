from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

MODEL_SCHEMA_VERSION = "phase4jv-startup-ordering-delay-model-v1"
STARTUP_DELAY_SECONDS = 120
REQUIRED_STAGES = (
    "HOST_BOOT",
    "STARTUP_DELAY",
    "TEST_HOST_PROHIBITION",
    "STATE_INTEGRITY_CHECK",
    "SINGLETON_LOCK_ACQUIRE",
    "READ_ONLY_HEALTH_OBSERVATION",
    "ALERT_ONLY",
)
ModelStatus = Literal["VALID", "DENIED", "INCOMPLETE", "TAMPERED"]


class StartupOrderingDelayModelError(ValueError):
    """Stable fail-closed startup ordering and delay model error."""


@dataclass(frozen=True)
class StartupOrderingRequest:
    proposal_hash: str
    identity_audit_hash: str
    configuration_hash: str
    stages: tuple[str, ...]
    startup_delay_seconds: int
    recovery_on_boot_requested: bool
    activation_requested: bool
    complete: bool
    request_hash: str


@dataclass(frozen=True)
class StartupOrderingDecision:
    status: ModelStatus
    reasons: tuple[str, ...]
    request_hash: str
    stages: tuple[str, ...]
    startup_delay_seconds: int
    ordering_hash: str
    decision_hash: str
    read_only: bool = True
    startup_model_valid: bool = False
    alert_only_after_boot: bool = True
    activation_authorized: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_startup_ordering_request(**fields: Any) -> StartupOrderingRequest:
    normalized = dict(fields)
    if isinstance(normalized.get("stages"), list):
        normalized["stages"] = tuple(normalized["stages"])
    _validate_fields(normalized)
    unsigned = {**normalized, "stages": list(normalized["stages"])}
    return StartupOrderingRequest(**normalized, request_hash=_hash(unsigned))


def evaluate_startup_ordering_delay(request: Any) -> StartupOrderingDecision:
    item = _validated_request(request)
    duplicates = len(set(item.stages)) != len(item.stages)
    unknown = sorted(set(item.stages) - set(REQUIRED_STAGES))
    if duplicates or unknown:
        status: ModelStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("STARTUP_ORDER_STAGE_DUPLICATE")
        reasons.extend(f"STARTUP_ORDER_STAGE_UNKNOWN:{stage}" for stage in unknown)
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["STARTUP_ORDER_REQUEST_INCOMPLETE"]
    else:
        reasons = []
        if item.stages != REQUIRED_STAGES:
            reasons.append("STARTUP_ORDER_SEQUENCE_INVALID")
        if item.startup_delay_seconds != STARTUP_DELAY_SECONDS:
            reasons.append("STARTUP_ORDER_DELAY_INVALID")
        if item.recovery_on_boot_requested:
            reasons.append("STARTUP_ORDER_RECOVERY_ON_BOOT_REFUSED")
        if item.activation_requested:
            reasons.append("STARTUP_ORDER_ACTIVATION_REFUSED")
        status = "DENIED" if reasons else "VALID"
    valid = status == "VALID"
    unsigned = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "request_hash": item.request_hash,
        "stages": list(REQUIRED_STAGES),
        "startup_delay_seconds": STARTUP_DELAY_SECONDS,
        "ordering_hash": _hash({"stages": list(REQUIRED_STAGES), "delay": STARTUP_DELAY_SECONDS}),
        "read_only": True,
        "startup_model_valid": valid,
        "alert_only_after_boot": True,
        "activation_authorized": False,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return StartupOrderingDecision(
        status=status,
        reasons=tuple(reasons),
        request_hash=item.request_hash,
        stages=REQUIRED_STAGES,
        startup_delay_seconds=STARTUP_DELAY_SECONDS,
        ordering_hash=unsigned["ordering_hash"],
        decision_hash=_hash(unsigned),
        startup_model_valid=valid,
    )


def validate_startup_ordering_decision(value: Any) -> None:
    if not isinstance(value, StartupOrderingDecision):
        raise StartupOrderingDelayModelError("STARTUP_ORDER_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.stages != REQUIRED_STAGES
        or value.startup_delay_seconds != STARTUP_DELAY_SECONDS
        or value.startup_model_valid != (value.status == "VALID")
        or value.alert_only_after_boot is not True
        or any(
            (
                value.activation_authorized,
                value.recovery_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise StartupOrderingDelayModelError("STARTUP_ORDER_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = MODEL_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["stages"] = list(unsigned["stages"])
    if value.decision_hash != _hash(unsigned):
        raise StartupOrderingDelayModelError("STARTUP_ORDER_DECISION_HASH_MISMATCH")


def _validated_request(value: Any) -> StartupOrderingRequest:
    if not isinstance(value, StartupOrderingRequest):
        raise StartupOrderingDelayModelError("STARTUP_ORDER_REQUEST_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("request_hash")
    unsigned["stages"] = tuple(unsigned["stages"])
    _validate_fields(unsigned)
    unsigned["stages"] = list(unsigned["stages"])
    if supplied != _hash(unsigned):
        raise StartupOrderingDelayModelError("STARTUP_ORDER_REQUEST_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "proposal_hash",
        "identity_audit_hash",
        "configuration_hash",
        "stages",
        "startup_delay_seconds",
        "recovery_on_boot_requested",
        "activation_requested",
        "complete",
    }
    if set(fields) != required:
        raise StartupOrderingDelayModelError("STARTUP_ORDER_REQUEST_FIELD_INVALID")
    for key in ("proposal_hash", "identity_audit_hash", "configuration_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise StartupOrderingDelayModelError("STARTUP_ORDER_REQUEST_FIELD_INVALID")
    if (
        not isinstance(fields["stages"], tuple)
        or len(fields["stages"]) > 16
        or any(
            not isinstance(stage, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", stage) is None
            for stage in fields["stages"]
        )
        or isinstance(fields["startup_delay_seconds"], bool)
        or not isinstance(fields["startup_delay_seconds"], int)
        or not 0 <= fields["startup_delay_seconds"] <= 600
    ):
        raise StartupOrderingDelayModelError("STARTUP_ORDER_REQUEST_FIELD_INVALID")
    for key in ("recovery_on_boot_requested", "activation_requested", "complete"):
        if not isinstance(fields[key], bool):
            raise StartupOrderingDelayModelError("STARTUP_ORDER_REQUEST_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
