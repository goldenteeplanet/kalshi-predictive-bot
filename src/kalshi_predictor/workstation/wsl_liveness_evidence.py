from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

EVIDENCE_SCHEMA_VERSION = "phase4hc-wsl-liveness-evidence-v1"
LivenessStatus = Literal["AVAILABLE", "UNAVAILABLE", "STALE", "INCOMPLETE"]


class WslLivenessEvidenceError(ValueError):
    """Stable fail-closed WSL liveness evidence error."""


@dataclass(frozen=True)
class WslLivenessProbeResult:
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    duration_milliseconds: int
    exit_code: int | None
    wsl_available: bool
    distribution_running: bool
    complete: bool
    probe_name: str
    source_identity_hash: str
    output_hash: str
    probe_result_hash: str


@dataclass(frozen=True)
class WslLivenessEvidence:
    status: LivenessStatus
    reasons: tuple[str, ...]
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    max_evidence_age_seconds: int
    duration_milliseconds: int
    max_duration_milliseconds: int
    exit_code: int | None
    probe_name: str
    source_identity_hash: str
    output_hash: str
    probe_result_hash: str
    evidence_hash: str
    read_only: bool = True
    alert_required: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_wsl_liveness_probe_result(
    *,
    observed_at_epoch_seconds: int,
    evidence_age_seconds: int,
    duration_milliseconds: int,
    exit_code: int | None,
    wsl_available: bool,
    distribution_running: bool,
    complete: bool,
    probe_name: str,
    source_identity_hash: str,
    output: str,
    max_output_characters: int = 4096,
) -> WslLivenessProbeResult:
    if not isinstance(output, str):
        raise WslLivenessEvidenceError("PROBE_OUTPUT_INVALID")
    if (
        isinstance(max_output_characters, bool)
        or not isinstance(max_output_characters, int)
        or max_output_characters <= 0
    ):
        raise WslLivenessEvidenceError("PROBE_OUTPUT_BOUND_INVALID")
    if len(output) > max_output_characters:
        raise WslLivenessEvidenceError("PROBE_OUTPUT_BOUND_EXCEEDED")
    unsigned = {
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "evidence_age_seconds": evidence_age_seconds,
        "duration_milliseconds": duration_milliseconds,
        "exit_code": exit_code,
        "wsl_available": wsl_available,
        "distribution_running": distribution_running,
        "complete": complete,
        "probe_name": probe_name,
        "source_identity_hash": source_identity_hash,
        "output_hash": _hash(output),
    }
    _validate_probe_fields(unsigned)
    return WslLivenessProbeResult(**unsigned, probe_result_hash=_hash(unsigned))


def collect_wsl_liveness_evidence(
    probe_result: Any,
    *,
    max_evidence_age_seconds: int = 120,
    max_duration_milliseconds: int = 5_000,
) -> WslLivenessEvidence:
    for bound in (max_evidence_age_seconds, max_duration_milliseconds):
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
            raise WslLivenessEvidenceError("EVIDENCE_BOUND_INVALID")
    probe = _validated_probe_result(probe_result)
    if probe.evidence_age_seconds > max_evidence_age_seconds:
        status: LivenessStatus = "STALE"
        reasons = ["WSL_LIVENESS_EVIDENCE_STALE"]
    elif not probe.complete:
        status = "INCOMPLETE"
        reasons = ["WSL_LIVENESS_PROBE_INCOMPLETE"]
    else:
        failures = []
        if probe.duration_milliseconds > max_duration_milliseconds:
            failures.append("WSL_LIVENESS_PROBE_TIMEOUT")
        if probe.exit_code != 0:
            failures.append("WSL_LIVENESS_PROBE_EXIT_NONZERO")
        if not probe.wsl_available:
            failures.append("WSL_UNAVAILABLE")
        if not probe.distribution_running:
            failures.append("WSL_DISTRIBUTION_NOT_RUNNING")
        status = "UNAVAILABLE" if failures else "AVAILABLE"
        reasons = sorted(failures)

    unsigned = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "observed_at_epoch_seconds": probe.observed_at_epoch_seconds,
        "evidence_age_seconds": probe.evidence_age_seconds,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "duration_milliseconds": probe.duration_milliseconds,
        "max_duration_milliseconds": max_duration_milliseconds,
        "exit_code": probe.exit_code,
        "probe_name": probe.probe_name,
        "source_identity_hash": probe.source_identity_hash,
        "output_hash": probe.output_hash,
        "probe_result_hash": probe.probe_result_hash,
        "read_only": True,
        "alert_required": status != "AVAILABLE",
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return WslLivenessEvidence(
        status=status,
        reasons=tuple(reasons),
        observed_at_epoch_seconds=probe.observed_at_epoch_seconds,
        evidence_age_seconds=probe.evidence_age_seconds,
        max_evidence_age_seconds=max_evidence_age_seconds,
        duration_milliseconds=probe.duration_milliseconds,
        max_duration_milliseconds=max_duration_milliseconds,
        exit_code=probe.exit_code,
        probe_name=probe.probe_name,
        source_identity_hash=probe.source_identity_hash,
        output_hash=probe.output_hash,
        probe_result_hash=probe.probe_result_hash,
        evidence_hash=_hash(unsigned),
        alert_required=unsigned["alert_required"],
    )


def validate_wsl_liveness_evidence(evidence: Any) -> None:
    if not isinstance(evidence, WslLivenessEvidence):
        raise WslLivenessEvidenceError("EVIDENCE_TYPE_INVALID")
    if evidence.read_only is not True or any(
        (
            evidence.recovery_authorized,
            evidence.service_control_authorized,
            evidence.host_restart_authorized,
            evidence.execution_authorized,
        )
    ):
        raise WslLivenessEvidenceError("EVIDENCE_SAFETY_BOUNDARY_INVALID")
    if (evidence.status == "AVAILABLE") != (not evidence.reasons and not evidence.alert_required):
        raise WslLivenessEvidenceError("EVIDENCE_STATUS_INVALID")
    if evidence.status != "AVAILABLE" and (not evidence.reasons or not evidence.alert_required):
        raise WslLivenessEvidenceError("EVIDENCE_STATUS_INVALID")
    unsigned = asdict(evidence)
    unsigned.pop("evidence_hash")
    unsigned["schema_version"] = EVIDENCE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if evidence.evidence_hash != _hash(unsigned):
        raise WslLivenessEvidenceError("EVIDENCE_HASH_MISMATCH")


def _validated_probe_result(value: Any) -> WslLivenessProbeResult:
    if not isinstance(value, WslLivenessProbeResult):
        raise WslLivenessEvidenceError("PROBE_RESULT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("probe_result_hash")
    _validate_probe_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise WslLivenessEvidenceError("PROBE_RESULT_HASH_MISMATCH")
    return value


def _validate_probe_fields(payload: dict[str, Any]) -> None:
    for key in ("wsl_available", "distribution_running", "complete"):
        if not isinstance(payload[key], bool):
            raise WslLivenessEvidenceError("PROBE_FIELD_INVALID")
    for key in ("probe_name", "source_identity_hash", "output_hash"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise WslLivenessEvidenceError("PROBE_FIELD_INVALID")
    for key in ("observed_at_epoch_seconds", "evidence_age_seconds", "duration_milliseconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WslLivenessEvidenceError("PROBE_FIELD_INVALID")
    exit_code = payload["exit_code"]
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise WslLivenessEvidenceError("PROBE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
