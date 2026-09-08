from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

PROBE_SCHEMA_VERSION = "phase4hn-windows-toast-capability-probe-v1"
CapabilityStatus = Literal["AVAILABLE", "UNAVAILABLE", "STALE", "INCOMPLETE"]


class WindowsToastCapabilityError(ValueError):
    """Stable fail-closed Windows toast capability evidence error."""


@dataclass(frozen=True)
class WindowsToastCapabilityObservation:
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    duration_milliseconds: int
    platform_name: str
    interactive_session: bool
    notifications_enabled: bool
    app_identity_registered: bool
    toast_api_available: bool
    complete: bool
    probe_name: str
    source_identity_hash: str
    output_hash: str
    observation_hash: str


@dataclass(frozen=True)
class WindowsToastCapabilityEvidence:
    status: CapabilityStatus
    reasons: tuple[str, ...]
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    max_evidence_age_seconds: int
    duration_milliseconds: int
    max_duration_milliseconds: int
    platform_name: str
    probe_name: str
    source_identity_hash: str
    output_hash: str
    observation_hash: str
    evidence_hash: str
    read_only: bool = True
    notification_sent: bool = False
    alert_delivery_authorized: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_windows_toast_capability_observation(
    *,
    observed_at_epoch_seconds: int,
    evidence_age_seconds: int,
    duration_milliseconds: int,
    platform_name: str,
    interactive_session: bool,
    notifications_enabled: bool,
    app_identity_registered: bool,
    toast_api_available: bool,
    complete: bool,
    probe_name: str,
    source_identity_hash: str,
    output: str,
    max_output_characters: int = 4096,
) -> WindowsToastCapabilityObservation:
    if not isinstance(output, str):
        raise WindowsToastCapabilityError("PROBE_OUTPUT_INVALID")
    if (
        isinstance(max_output_characters, bool)
        or not isinstance(max_output_characters, int)
        or max_output_characters <= 0
    ):
        raise WindowsToastCapabilityError("PROBE_OUTPUT_BOUND_INVALID")
    if len(output) > max_output_characters:
        raise WindowsToastCapabilityError("PROBE_OUTPUT_BOUND_EXCEEDED")
    unsigned = {
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "evidence_age_seconds": evidence_age_seconds,
        "duration_milliseconds": duration_milliseconds,
        "platform_name": platform_name,
        "interactive_session": interactive_session,
        "notifications_enabled": notifications_enabled,
        "app_identity_registered": app_identity_registered,
        "toast_api_available": toast_api_available,
        "complete": complete,
        "probe_name": probe_name,
        "source_identity_hash": source_identity_hash,
        "output_hash": _hash(output),
    }
    _validate_observation_fields(unsigned)
    return WindowsToastCapabilityObservation(**unsigned, observation_hash=_hash(unsigned))


def probe_windows_toast_capability(
    observation: Any,
    *,
    max_evidence_age_seconds: int = 300,
    max_duration_milliseconds: int = 5_000,
) -> WindowsToastCapabilityEvidence:
    for bound in (max_evidence_age_seconds, max_duration_milliseconds):
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
            raise WindowsToastCapabilityError("PROBE_BOUND_INVALID")
    item = _validated_observation(observation)
    if item.evidence_age_seconds > max_evidence_age_seconds:
        status: CapabilityStatus = "STALE"
        reasons = ["WINDOWS_TOAST_CAPABILITY_EVIDENCE_STALE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["WINDOWS_TOAST_CAPABILITY_PROBE_INCOMPLETE"]
    else:
        failures = []
        if item.duration_milliseconds > max_duration_milliseconds:
            failures.append("WINDOWS_TOAST_CAPABILITY_PROBE_TIMEOUT")
        if item.platform_name != "Windows":
            failures.append("WINDOWS_PLATFORM_REQUIRED")
        if not item.interactive_session:
            failures.append("INTERACTIVE_SESSION_MISSING")
        if not item.notifications_enabled:
            failures.append("WINDOWS_NOTIFICATIONS_DISABLED")
        if not item.app_identity_registered:
            failures.append("TOAST_APP_IDENTITY_MISSING")
        if not item.toast_api_available:
            failures.append("WINDOWS_TOAST_API_UNAVAILABLE")
        status = "UNAVAILABLE" if failures else "AVAILABLE"
        reasons = sorted(failures)

    unsigned = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "observed_at_epoch_seconds": item.observed_at_epoch_seconds,
        "evidence_age_seconds": item.evidence_age_seconds,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "duration_milliseconds": item.duration_milliseconds,
        "max_duration_milliseconds": max_duration_milliseconds,
        "platform_name": item.platform_name,
        "probe_name": item.probe_name,
        "source_identity_hash": item.source_identity_hash,
        "output_hash": item.output_hash,
        "observation_hash": item.observation_hash,
        "read_only": True,
        "notification_sent": False,
        "alert_delivery_authorized": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return WindowsToastCapabilityEvidence(
        status=status,
        reasons=tuple(reasons),
        observed_at_epoch_seconds=item.observed_at_epoch_seconds,
        evidence_age_seconds=item.evidence_age_seconds,
        max_evidence_age_seconds=max_evidence_age_seconds,
        duration_milliseconds=item.duration_milliseconds,
        max_duration_milliseconds=max_duration_milliseconds,
        platform_name=item.platform_name,
        probe_name=item.probe_name,
        source_identity_hash=item.source_identity_hash,
        output_hash=item.output_hash,
        observation_hash=item.observation_hash,
        evidence_hash=_hash(unsigned),
    )


def validate_windows_toast_capability_evidence(evidence: Any) -> None:
    if not isinstance(evidence, WindowsToastCapabilityEvidence):
        raise WindowsToastCapabilityError("EVIDENCE_TYPE_INVALID")
    if evidence.read_only is not True or any(
        (
            evidence.notification_sent,
            evidence.alert_delivery_authorized,
            evidence.recovery_authorized,
            evidence.service_control_authorized,
            evidence.host_restart_authorized,
            evidence.execution_authorized,
        )
    ):
        raise WindowsToastCapabilityError("EVIDENCE_SAFETY_BOUNDARY_INVALID")
    available = not evidence.reasons
    if (evidence.status == "AVAILABLE") != available:
        raise WindowsToastCapabilityError("EVIDENCE_STATUS_INVALID")
    unsigned = asdict(evidence)
    unsigned.pop("evidence_hash")
    unsigned["schema_version"] = PROBE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if evidence.evidence_hash != _hash(unsigned):
        raise WindowsToastCapabilityError("EVIDENCE_HASH_MISMATCH")


def _validated_observation(value: Any) -> WindowsToastCapabilityObservation:
    if not isinstance(value, WindowsToastCapabilityObservation):
        raise WindowsToastCapabilityError("OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("observation_hash")
    _validate_observation_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise WindowsToastCapabilityError("OBSERVATION_HASH_MISMATCH")
    return value


def _validate_observation_fields(payload: dict[str, Any]) -> None:
    for key in (
        "interactive_session",
        "notifications_enabled",
        "app_identity_registered",
        "toast_api_available",
        "complete",
    ):
        if not isinstance(payload[key], bool):
            raise WindowsToastCapabilityError("OBSERVATION_FIELD_INVALID")
    for key in ("platform_name", "probe_name", "source_identity_hash", "output_hash"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise WindowsToastCapabilityError("OBSERVATION_FIELD_INVALID")
    for key in ("observed_at_epoch_seconds", "evidence_age_seconds", "duration_milliseconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WindowsToastCapabilityError("OBSERVATION_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
