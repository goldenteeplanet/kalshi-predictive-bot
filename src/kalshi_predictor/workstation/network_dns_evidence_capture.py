from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

CAPTURE_SCHEMA_VERSION = "phase4il-network-dns-evidence-capture-v1"
ProbeType = Literal["DNS", "ROUTE", "TCP", "TLS", "HTTP"]
CaptureStatus = Literal["CAPTURED", "PARTIAL", "TAMPERED", "REFUSED"]


class NetworkDnsEvidenceCaptureError(ValueError):
    """Stable fail-closed network and DNS evidence capture error."""


@dataclass(frozen=True)
class NetworkProbeEvidence:
    probe_id_hash: str
    target_id_hash: str
    probe_type: ProbeType
    observed_at_epoch_seconds: int
    succeeded: bool
    latency_milliseconds: int | None
    result_code: str
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class NetworkDnsCapture:
    status: CaptureStatus
    reasons: tuple[str, ...]
    window_start_epoch_seconds: int
    window_end_epoch_seconds: int
    probe_count: int
    success_count: int
    failure_count: int
    probe_type_counts: tuple[tuple[str, int], ...]
    probe_hashes: tuple[str, ...]
    capture_hash: str
    read_only: bool = True
    targets_redacted: bool = True
    raw_responses_retained: bool = False
    network_evidence_complete: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_network_probe_evidence(**fields: Any) -> NetworkProbeEvidence:
    _validate_fields(fields)
    return NetworkProbeEvidence(**fields, evidence_hash=_hash(fields))


def capture_network_dns_evidence(
    probes: Sequence[Any],
    *,
    window_start_epoch_seconds: int,
    window_end_epoch_seconds: int,
    max_probes: int = 64,
) -> NetworkDnsCapture:
    for value in (window_start_epoch_seconds, window_end_epoch_seconds, max_probes):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise NetworkDnsEvidenceCaptureError("NETWORK_CAPTURE_BOUND_INVALID")
    if window_start_epoch_seconds > window_end_epoch_seconds or max_probes == 0:
        raise NetworkDnsEvidenceCaptureError("NETWORK_CAPTURE_BOUND_INVALID")
    if isinstance(probes, str | bytes) or len(probes) > max_probes:
        raise NetworkDnsEvidenceCaptureError("NETWORK_PROBE_BOUND_EXCEEDED")
    records = [_validated_probe(item) for item in probes]
    records.sort(key=lambda item: (item.observed_at_epoch_seconds, item.probe_id_hash))
    ids = [item.probe_id_hash for item in records]
    outside = any(
        not window_start_epoch_seconds <= item.observed_at_epoch_seconds <= window_end_epoch_seconds
        for item in records
    )
    contradictory = any(
        (item.succeeded and item.result_code != "SUCCESS")
        or (not item.succeeded and item.result_code == "SUCCESS")
        for item in records
    )
    if len(set(ids)) != len(ids) or contradictory:
        status: CaptureStatus = "TAMPERED"
        reasons = ["NETWORK_PROBE_DUPLICATE_OR_CONTRADICTORY"]
    elif outside:
        status = "REFUSED"
        reasons = ["NETWORK_PROBE_OUTSIDE_WINDOW"]
    elif not records or any(not item.complete for item in records):
        status = "PARTIAL"
        reasons = ["NETWORK_PROBE_EMPTY_OR_INCOMPLETE"]
    else:
        status = "CAPTURED"
        reasons = []
    complete = status == "CAPTURED"
    hashes = tuple(item.evidence_hash for item in records)
    types = tuple(
        (kind, sum(item.probe_type == kind for item in records))
        for kind in ("DNS", "ROUTE", "TCP", "TLS", "HTTP")
    )
    successes = sum(item.succeeded for item in records)
    unsigned = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "window_start_epoch_seconds": window_start_epoch_seconds,
        "window_end_epoch_seconds": window_end_epoch_seconds,
        "probe_count": len(records),
        "success_count": successes,
        "failure_count": len(records) - successes,
        "probe_type_counts": [list(item) for item in types],
        "probe_hashes": list(hashes),
        "read_only": True,
        "targets_redacted": True,
        "raw_responses_retained": False,
        "network_evidence_complete": complete,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return NetworkDnsCapture(
        status=status,
        reasons=tuple(reasons),
        window_start_epoch_seconds=window_start_epoch_seconds,
        window_end_epoch_seconds=window_end_epoch_seconds,
        probe_count=len(records),
        success_count=successes,
        failure_count=len(records) - successes,
        probe_type_counts=types,
        probe_hashes=hashes,
        capture_hash=_hash(unsigned),
        network_evidence_complete=complete,
    )


def validate_network_dns_capture(value: Any) -> None:
    if not isinstance(value, NetworkDnsCapture):
        raise NetworkDnsEvidenceCaptureError("NETWORK_CAPTURE_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.targets_redacted is not True
        or value.raw_responses_retained is not False
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise NetworkDnsEvidenceCaptureError("NETWORK_CAPTURE_SAFETY_BOUNDARY_INVALID")
    if value.network_evidence_complete != (value.status == "CAPTURED"):
        raise NetworkDnsEvidenceCaptureError("NETWORK_CAPTURE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("capture_hash")
    unsigned["schema_version"] = CAPTURE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["probe_type_counts"] = [list(item) for item in unsigned["probe_type_counts"]]
    unsigned["probe_hashes"] = list(unsigned["probe_hashes"])
    if value.capture_hash != _hash(unsigned):
        raise NetworkDnsEvidenceCaptureError("NETWORK_CAPTURE_HASH_MISMATCH")


def _validated_probe(value: Any) -> NetworkProbeEvidence:
    if not isinstance(value, NetworkProbeEvidence):
        raise NetworkDnsEvidenceCaptureError("NETWORK_PROBE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise NetworkDnsEvidenceCaptureError("NETWORK_PROBE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "probe_id_hash",
        "target_id_hash",
        "probe_type",
        "observed_at_epoch_seconds",
        "succeeded",
        "latency_milliseconds",
        "result_code",
        "complete",
    }
    if set(fields) != required:
        raise NetworkDnsEvidenceCaptureError("NETWORK_PROBE_FIELD_INVALID")
    for key in ("probe_id_hash", "target_id_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise NetworkDnsEvidenceCaptureError("NETWORK_PROBE_FIELD_INVALID")
    if fields["probe_type"] not in {"DNS", "ROUTE", "TCP", "TLS", "HTTP"}:
        raise NetworkDnsEvidenceCaptureError("NETWORK_PROBE_FIELD_INVALID")
    for key in ("observed_at_epoch_seconds", "latency_milliseconds"):
        item = fields[key]
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
            raise NetworkDnsEvidenceCaptureError("NETWORK_PROBE_FIELD_INVALID")
    if fields["observed_at_epoch_seconds"] is None:
        raise NetworkDnsEvidenceCaptureError("NETWORK_PROBE_FIELD_INVALID")
    if (
        not isinstance(fields["result_code"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["result_code"]) is None
    ):
        raise NetworkDnsEvidenceCaptureError("NETWORK_PROBE_FIELD_INVALID")
    if not isinstance(fields["succeeded"], bool) or not isinstance(fields["complete"], bool):
        raise NetworkDnsEvidenceCaptureError("NETWORK_PROBE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
