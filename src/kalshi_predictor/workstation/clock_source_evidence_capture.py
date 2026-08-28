from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

CAPTURE_SCHEMA_VERSION = "phase4im-clock-source-evidence-capture-v1"
SourceType = Literal["NTP", "WINDOWS_HOST", "SIGNED_TIME"]
CaptureStatus = Literal["CAPTURED", "PARTIAL", "TAMPERED", "REFUSED"]


class ClockSourceEvidenceCaptureError(ValueError):
    """Stable fail-closed clock-source evidence capture error."""


@dataclass(frozen=True)
class ClockSourceEvidence:
    source_id_hash: str
    source_type: SourceType
    observed_at_epoch_seconds: int
    offset_milliseconds: int
    uncertainty_milliseconds: int
    stratum: int | None
    synchronized: bool
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class ClockSourceCapture:
    status: CaptureStatus
    reasons: tuple[str, ...]
    captured_at_epoch_seconds: int
    source_count: int
    synchronized_source_count: int
    maximum_absolute_offset_milliseconds: int
    maximum_uncertainty_milliseconds: int
    source_hashes: tuple[str, ...]
    capture_hash: str
    read_only: bool = True
    source_addresses_redacted: bool = True
    raw_output_retained: bool = False
    clock_source_evidence_complete: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_clock_source_evidence(**fields: Any) -> ClockSourceEvidence:
    _validate_fields(fields)
    return ClockSourceEvidence(**fields, evidence_hash=_hash(fields))


def capture_clock_source_evidence(
    sources: Sequence[Any],
    *,
    captured_at_epoch_seconds: int,
    max_sources: int = 8,
    maximum_absolute_offset_milliseconds: int = 86_400_000,
) -> ClockSourceCapture:
    for value in (captured_at_epoch_seconds, max_sources, maximum_absolute_offset_milliseconds):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_BOUND_INVALID")
    if isinstance(sources, (str, bytes)) or len(sources) > max_sources:
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_COUNT_BOUND_EXCEEDED")
    records = [_validated_source(item) for item in sources]
    records.sort(key=lambda item: (item.source_type, item.source_id_hash))
    ids = [item.source_id_hash for item in records]
    future = any(item.observed_at_epoch_seconds > captured_at_epoch_seconds for item in records)
    excessive = any(
        abs(item.offset_milliseconds) > maximum_absolute_offset_milliseconds for item in records
    )
    if len(set(ids)) != len(ids) or future:
        status: CaptureStatus = "TAMPERED"
        reasons = ["CLOCK_SOURCE_DUPLICATE_OR_FUTURE"]
    elif excessive:
        status = "REFUSED"
        reasons = ["CLOCK_SOURCE_OFFSET_BOUND_EXCEEDED"]
    elif not records or any(not item.complete for item in records):
        status = "PARTIAL"
        reasons = ["CLOCK_SOURCE_EVIDENCE_EMPTY_OR_INCOMPLETE"]
    else:
        status = "CAPTURED"
        reasons = []
    complete = status == "CAPTURED"
    hashes = tuple(item.evidence_hash for item in records)
    synchronized_count = sum(item.synchronized for item in records)
    max_offset = max((abs(item.offset_milliseconds) for item in records), default=0)
    max_uncertainty = max((item.uncertainty_milliseconds for item in records), default=0)
    unsigned = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "captured_at_epoch_seconds": captured_at_epoch_seconds,
        "source_count": len(records),
        "synchronized_source_count": synchronized_count,
        "maximum_absolute_offset_milliseconds": max_offset,
        "maximum_uncertainty_milliseconds": max_uncertainty,
        "source_hashes": list(hashes),
        "read_only": True,
        "source_addresses_redacted": True,
        "raw_output_retained": False,
        "clock_source_evidence_complete": complete,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return ClockSourceCapture(
        status=status,
        reasons=tuple(reasons),
        captured_at_epoch_seconds=captured_at_epoch_seconds,
        source_count=len(records),
        synchronized_source_count=synchronized_count,
        maximum_absolute_offset_milliseconds=max_offset,
        maximum_uncertainty_milliseconds=max_uncertainty,
        source_hashes=hashes,
        capture_hash=_hash(unsigned),
        clock_source_evidence_complete=complete,
    )


def validate_clock_source_capture(value: Any) -> None:
    if not isinstance(value, ClockSourceCapture):
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_CAPTURE_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.source_addresses_redacted is not True
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
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_SAFETY_BOUNDARY_INVALID")
    if value.clock_source_evidence_complete != (value.status == "CAPTURED"):
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("capture_hash")
    unsigned["schema_version"] = CAPTURE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["source_hashes"] = list(unsigned["source_hashes"])
    if value.capture_hash != _hash(unsigned):
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_CAPTURE_HASH_MISMATCH")


def _validated_source(value: Any) -> ClockSourceEvidence:
    if not isinstance(value, ClockSourceEvidence):
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "source_id_hash",
        "source_type",
        "observed_at_epoch_seconds",
        "offset_milliseconds",
        "uncertainty_milliseconds",
        "stratum",
        "synchronized",
        "complete",
    }
    if (
        set(fields) != required
        or not isinstance(fields["source_id_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["source_id_hash"]) is None
    ):
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_FIELD_INVALID")
    if fields["source_type"] not in {"NTP", "WINDOWS_HOST", "SIGNED_TIME"}:
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_FIELD_INVALID")
    for key in ("observed_at_epoch_seconds", "offset_milliseconds", "uncertainty_milliseconds"):
        item = fields[key]
        if isinstance(item, bool) or not isinstance(item, int):
            raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_FIELD_INVALID")
    if fields["observed_at_epoch_seconds"] < 0 or fields["uncertainty_milliseconds"] < 0:
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_FIELD_INVALID")
    stratum = fields["stratum"]
    if stratum is not None and (
        isinstance(stratum, bool) or not isinstance(stratum, int) or not 0 <= stratum <= 16
    ):
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_FIELD_INVALID")
    if fields["source_type"] != "NTP" and stratum is not None:
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_FIELD_INVALID")
    if not isinstance(fields["synchronized"], bool) or not isinstance(fields["complete"], bool):
        raise ClockSourceEvidenceCaptureError("CLOCK_SOURCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
