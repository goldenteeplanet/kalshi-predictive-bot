from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

CAPTURE_SCHEMA_VERSION = "phase4ih-wsl-status-evidence-capture-v1"
DistributionState = Literal["RUNNING", "STOPPED", "INSTALLING", "UNAVAILABLE", "UNKNOWN"]
CaptureStatus = Literal["CAPTURED", "PARTIAL", "TAMPERED", "REFUSED"]


class WslStatusEvidenceCaptureError(ValueError):
    """Stable fail-closed WSL status evidence capture error."""


@dataclass(frozen=True)
class WslDistributionEvidence:
    distribution_id_hash: str
    state: DistributionState
    wsl_version: int
    is_default: bool
    observed_at_epoch_seconds: int
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class WslStatusCapture:
    status: CaptureStatus
    reasons: tuple[str, ...]
    captured_at_epoch_seconds: int
    distribution_count: int
    running_count: int
    stopped_count: int
    default_distribution_count: int
    distribution_hashes: tuple[str, ...]
    capture_hash: str
    read_only: bool = True
    distribution_names_redacted: bool = True
    raw_output_retained: bool = False
    wsl_status_complete: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_wsl_distribution_evidence(**fields: Any) -> WslDistributionEvidence:
    _validate_fields(fields)
    return WslDistributionEvidence(**fields, evidence_hash=_hash(fields))


def capture_wsl_status_evidence(
    distributions: Sequence[Any],
    *,
    captured_at_epoch_seconds: int,
    max_distributions: int = 16,
) -> WslStatusCapture:
    for value in (captured_at_epoch_seconds, max_distributions):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise WslStatusEvidenceCaptureError("WSL_STATUS_BOUND_INVALID")
    if isinstance(distributions, (str, bytes)) or len(distributions) > max_distributions:
        raise WslStatusEvidenceCaptureError("WSL_DISTRIBUTION_BOUND_EXCEEDED")
    records = [_validated_evidence(item) for item in distributions]
    records.sort(key=lambda item: item.distribution_id_hash)
    ids = [item.distribution_id_hash for item in records]
    default_count = sum(item.is_default for item in records)
    future = any(item.observed_at_epoch_seconds > captured_at_epoch_seconds for item in records)
    if len(set(ids)) != len(ids) or default_count > 1:
        status: CaptureStatus = "TAMPERED"
        reasons = ["WSL_STATUS_DUPLICATE_OR_MULTIPLE_DEFAULTS"]
    elif future:
        status = "TAMPERED"
        reasons = ["WSL_STATUS_EVIDENCE_FROM_FUTURE"]
    elif not records:
        status = "PARTIAL"
        reasons = ["WSL_DISTRIBUTION_EVIDENCE_EMPTY"]
    elif any(not item.complete for item in records) or default_count != 1:
        status = "PARTIAL"
        reasons = ["WSL_STATUS_EVIDENCE_INCOMPLETE_OR_DEFAULT_MISSING"]
    else:
        status = "CAPTURED"
        reasons = []
    complete = status == "CAPTURED"
    hashes = tuple(item.evidence_hash for item in records)
    running = sum(item.state == "RUNNING" for item in records)
    stopped = sum(item.state == "STOPPED" for item in records)
    unsigned = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "captured_at_epoch_seconds": captured_at_epoch_seconds,
        "distribution_count": len(records),
        "running_count": running,
        "stopped_count": stopped,
        "default_distribution_count": default_count,
        "distribution_hashes": list(hashes),
        "read_only": True,
        "distribution_names_redacted": True,
        "raw_output_retained": False,
        "wsl_status_complete": complete,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return WslStatusCapture(
        status=status,
        reasons=tuple(reasons),
        captured_at_epoch_seconds=captured_at_epoch_seconds,
        distribution_count=len(records),
        running_count=running,
        stopped_count=stopped,
        default_distribution_count=default_count,
        distribution_hashes=hashes,
        capture_hash=_hash(unsigned),
        wsl_status_complete=complete,
    )


def validate_wsl_status_capture(value: Any) -> None:
    if not isinstance(value, WslStatusCapture):
        raise WslStatusEvidenceCaptureError("WSL_STATUS_CAPTURE_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.distribution_names_redacted is not True
        or value.raw_output_retained is not False
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise WslStatusEvidenceCaptureError("WSL_STATUS_SAFETY_BOUNDARY_INVALID")
    if value.wsl_status_complete != (value.status == "CAPTURED"):
        raise WslStatusEvidenceCaptureError("WSL_STATUS_STATE_INVALID")
    unsigned = asdict(value)
    unsigned.pop("capture_hash")
    unsigned["schema_version"] = CAPTURE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["distribution_hashes"] = list(unsigned["distribution_hashes"])
    if value.capture_hash != _hash(unsigned):
        raise WslStatusEvidenceCaptureError("WSL_STATUS_CAPTURE_HASH_MISMATCH")


def _validated_evidence(value: Any) -> WslDistributionEvidence:
    if not isinstance(value, WslDistributionEvidence):
        raise WslStatusEvidenceCaptureError("WSL_DISTRIBUTION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise WslStatusEvidenceCaptureError("WSL_DISTRIBUTION_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "distribution_id_hash",
        "state",
        "wsl_version",
        "is_default",
        "observed_at_epoch_seconds",
        "complete",
    }
    if (
        set(fields) != required
        or not isinstance(fields["distribution_id_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["distribution_id_hash"]) is None
    ):
        raise WslStatusEvidenceCaptureError("WSL_DISTRIBUTION_FIELD_INVALID")
    if fields["state"] not in {"RUNNING", "STOPPED", "INSTALLING", "UNAVAILABLE", "UNKNOWN"}:
        raise WslStatusEvidenceCaptureError("WSL_DISTRIBUTION_FIELD_INVALID")
    if isinstance(fields["wsl_version"], bool) or fields["wsl_version"] not in {1, 2}:
        raise WslStatusEvidenceCaptureError("WSL_DISTRIBUTION_FIELD_INVALID")
    timestamp = fields["observed_at_epoch_seconds"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise WslStatusEvidenceCaptureError("WSL_DISTRIBUTION_FIELD_INVALID")
    if not isinstance(fields["is_default"], bool) or not isinstance(fields["complete"], bool):
        raise WslStatusEvidenceCaptureError("WSL_DISTRIBUTION_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
