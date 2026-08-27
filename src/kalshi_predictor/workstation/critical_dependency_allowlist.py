from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

ALLOWLIST_SCHEMA_VERSION = "phase4hv-critical-dependency-allowlist-v1"
ALLOWLISTED_DEPENDENCIES = (
    "KALSHI_SCHEDULER",
    "SYSTEMD_USER_MANAGER",
    "WSL_VM",
)
DependencyStatus = Literal["ALLOWLISTED", "DENIED", "INCOMPLETE"]


class CriticalDependencyAllowlistError(ValueError):
    """Stable fail-closed critical dependency allowlist error."""


@dataclass(frozen=True)
class DependencyObservation:
    observation_id_hash: str
    dependency_code: str
    observed_at_epoch_seconds: int
    complete: bool
    observation_hash: str


@dataclass(frozen=True)
class CriticalDependencyDecision:
    status: DependencyStatus
    reasons: tuple[str, ...]
    dependency_code: str
    observation_hash: str
    evaluated_at_epoch_seconds: int
    allowlist_hash: str
    decision_hash: str
    read_only: bool = True
    dependency_allowlisted: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_dependency_observation(
    *,
    observation_id_hash: str,
    dependency_code: str,
    observed_at_epoch_seconds: int,
    complete: bool,
) -> DependencyObservation:
    unsigned = {
        "observation_id_hash": observation_id_hash,
        "dependency_code": dependency_code,
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "complete": complete,
    }
    _validate_observation_fields(unsigned)
    return DependencyObservation(**unsigned, observation_hash=_hash(unsigned))


def evaluate_critical_dependency(
    observation: Any,
    *,
    evaluated_at_epoch_seconds: int,
) -> CriticalDependencyDecision:
    if (
        isinstance(evaluated_at_epoch_seconds, bool)
        or not isinstance(evaluated_at_epoch_seconds, int)
        or evaluated_at_epoch_seconds < 0
    ):
        raise CriticalDependencyAllowlistError("DEPENDENCY_BOUND_INVALID")
    item = _validated_observation(observation)
    if item.observed_at_epoch_seconds > evaluated_at_epoch_seconds:
        status: DependencyStatus = "DENIED"
        reasons = ["DEPENDENCY_OBSERVATION_FROM_FUTURE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["DEPENDENCY_OBSERVATION_INCOMPLETE"]
    elif item.dependency_code not in ALLOWLISTED_DEPENDENCIES:
        status = "DENIED"
        reasons = [f"DEPENDENCY_NOT_ALLOWLISTED:{item.dependency_code}"]
    else:
        status = "ALLOWLISTED"
        reasons = []
    allowlisted = status == "ALLOWLISTED"
    allowlist_hash = _hash(list(ALLOWLISTED_DEPENDENCIES))
    unsigned = {
        "schema_version": ALLOWLIST_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "dependency_code": item.dependency_code,
        "observation_hash": item.observation_hash,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "allowlist_hash": allowlist_hash,
        "read_only": True,
        "dependency_allowlisted": allowlisted,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return CriticalDependencyDecision(
        status=status,
        reasons=tuple(reasons),
        dependency_code=item.dependency_code,
        observation_hash=item.observation_hash,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        allowlist_hash=allowlist_hash,
        decision_hash=_hash(unsigned),
        dependency_allowlisted=allowlisted,
    )


def validate_critical_dependency_decision(value: Any) -> None:
    if not isinstance(value, CriticalDependencyDecision):
        raise CriticalDependencyAllowlistError("DEPENDENCY_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise CriticalDependencyAllowlistError("DEPENDENCY_SAFETY_BOUNDARY_INVALID")
    if value.dependency_allowlisted != (value.status == "ALLOWLISTED"):
        raise CriticalDependencyAllowlistError("DEPENDENCY_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = ALLOWLIST_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise CriticalDependencyAllowlistError("DEPENDENCY_DECISION_HASH_MISMATCH")


def _validated_observation(value: Any) -> DependencyObservation:
    if not isinstance(value, DependencyObservation):
        raise CriticalDependencyAllowlistError("DEPENDENCY_OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("observation_hash")
    _validate_observation_fields(unsigned)
    if supplied != _hash(unsigned):
        raise CriticalDependencyAllowlistError("DEPENDENCY_OBSERVATION_HASH_MISMATCH")
    return value


def _validate_observation_fields(fields: dict[str, Any]) -> None:
    if (
        not isinstance(fields["observation_id_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["observation_id_hash"]) is None
    ):
        raise CriticalDependencyAllowlistError("DEPENDENCY_OBSERVATION_FIELD_INVALID")
    if (
        not isinstance(fields["dependency_code"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["dependency_code"]) is None
    ):
        raise CriticalDependencyAllowlistError("DEPENDENCY_OBSERVATION_FIELD_INVALID")
    observed = fields["observed_at_epoch_seconds"]
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise CriticalDependencyAllowlistError("DEPENDENCY_OBSERVATION_FIELD_INVALID")
    if not isinstance(fields["complete"], bool):
        raise CriticalDependencyAllowlistError("DEPENDENCY_OBSERVATION_FIELD_INVALID")


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
