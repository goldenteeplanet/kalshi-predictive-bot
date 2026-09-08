from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

STALENESS_SCHEMA_VERSION = "phase4fu-read-model-staleness-v1"
Status = Literal["FRESH", "WARNING", "STALE", "STALLED", "LINEAGE_FAILURE"]


class StalenessEscalationError(ValueError):
    """Stable fail-closed staleness evidence rejection."""


@dataclass(frozen=True)
class StalenessEscalation:
    status: Status
    action: str
    snapshot_age_seconds: float
    progress_age_seconds: float
    reason: str
    evidence_hash: str


def build_staleness_evidence(
    *,
    snapshot_generated_at: datetime,
    last_progress_at: datetime,
    lineage_valid: bool,
    source_identity_hash: str,
    watermark: str,
) -> dict[str, Any]:
    _require_aware(snapshot_generated_at, "SNAPSHOT_TIMESTAMP_NAIVE")
    _require_aware(last_progress_at, "PROGRESS_TIMESTAMP_NAIVE")
    if not isinstance(lineage_valid, bool):
        raise StalenessEscalationError("LINEAGE_FLAG_INVALID")
    _require_hash(source_identity_hash, "SOURCE_IDENTITY_HASH_INVALID")
    if not watermark:
        raise StalenessEscalationError("WATERMARK_EMPTY")
    payload: dict[str, Any] = {
        "schema_version": STALENESS_SCHEMA_VERSION,
        "snapshot_generated_at": snapshot_generated_at.astimezone(UTC).isoformat(),
        "last_progress_at": last_progress_at.astimezone(UTC).isoformat(),
        "lineage_valid": lineage_valid,
        "source_identity_hash": source_identity_hash,
        "watermark": watermark,
    }
    payload["evidence_hash"] = _hash(payload)
    return payload


def classify_staleness(
    evidence: Any,
    *,
    now: datetime,
    warning_age_seconds: float,
    stale_age_seconds: float,
    stall_age_seconds: float,
) -> StalenessEscalation:
    _require_aware(now, "NOW_NAIVE")
    if not 0 < warning_age_seconds < stale_age_seconds or stall_age_seconds <= 0:
        raise StalenessEscalationError("THRESHOLD_INVALID")
    row = _validate(evidence)
    snapshot_age = (now.astimezone(UTC) - _timestamp(row, "snapshot_generated_at")).total_seconds()
    progress_age = (now.astimezone(UTC) - _timestamp(row, "last_progress_at")).total_seconds()
    if snapshot_age < 0:
        raise StalenessEscalationError("SNAPSHOT_FUTURE_DATED")
    if progress_age < 0:
        raise StalenessEscalationError("PROGRESS_FUTURE_DATED")
    if not row["lineage_valid"]:
        return _result(
            "LINEAGE_FAILURE",
            "ESCALATE_CRITICAL",
            snapshot_age,
            progress_age,
            "hash or provenance lineage is invalid",
            row,
        )
    if progress_age >= stall_age_seconds:
        return _result(
            "STALLED",
            "ESCALATE_RECONCILIATION",
            snapshot_age,
            progress_age,
            "watermark has not progressed inside the stall threshold",
            row,
        )
    if snapshot_age >= stale_age_seconds:
        return _result(
            "STALE",
            "ESCALATE_SOURCE_STALENESS",
            snapshot_age,
            progress_age,
            "snapshot age reached the stale threshold",
            row,
        )
    if snapshot_age >= warning_age_seconds:
        return _result(
            "WARNING",
            "MONITOR_CLOSELY",
            snapshot_age,
            progress_age,
            "snapshot age reached the warning threshold",
            row,
        )
    return _result(
        "FRESH",
        "NO_ACTION",
        snapshot_age,
        progress_age,
        "snapshot and progress evidence are inside thresholds",
        row,
    )


def _result(
    status: Status,
    action: str,
    snapshot_age: float,
    progress_age: float,
    reason: str,
    evidence: dict[str, Any],
) -> StalenessEscalation:
    return StalenessEscalation(
        status=status,
        action=action,
        snapshot_age_seconds=snapshot_age,
        progress_age_seconds=progress_age,
        reason=reason,
        evidence_hash=evidence["evidence_hash"],
    )


def _validate(payload: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "snapshot_generated_at",
        "last_progress_at",
        "lineage_valid",
        "source_identity_hash",
        "watermark",
        "evidence_hash",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise StalenessEscalationError("EVIDENCE_FIELDS_INVALID")
    if payload["schema_version"] != STALENESS_SCHEMA_VERSION:
        raise StalenessEscalationError("EVIDENCE_SCHEMA_UNSUPPORTED")
    if not isinstance(payload["lineage_valid"], bool):
        raise StalenessEscalationError("LINEAGE_FLAG_INVALID")
    _require_hash(payload["source_identity_hash"], "SOURCE_IDENTITY_HASH_INVALID")
    if not isinstance(payload["watermark"], str) or not payload["watermark"]:
        raise StalenessEscalationError("WATERMARK_EMPTY")
    _timestamp(payload, "snapshot_generated_at")
    _timestamp(payload, "last_progress_at")
    unhashed = {key: value for key, value in payload.items() if key != "evidence_hash"}
    if payload["evidence_hash"] != _hash(unhashed):
        raise StalenessEscalationError("EVIDENCE_HASH_MISMATCH")
    return payload


def _timestamp(payload: dict[str, Any], key: str) -> datetime:
    value = payload[key]
    if not isinstance(value, str):
        raise StalenessEscalationError("EVIDENCE_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StalenessEscalationError("EVIDENCE_TIMESTAMP_INVALID") from exc
    _require_aware(parsed, "EVIDENCE_TIMESTAMP_NAIVE")
    return parsed.astimezone(UTC)


def _require_aware(value: datetime, reason: str) -> None:
    if value.tzinfo is None:
        raise StalenessEscalationError(reason)


def _require_hash(value: Any, reason: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise StalenessEscalationError(reason)
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise StalenessEscalationError(reason) from exc


def _hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
