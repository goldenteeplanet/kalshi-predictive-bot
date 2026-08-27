from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

WATERMARK_SCHEMA_VERSION = "phase4fp-read-model-watermark-v1"
Transition = Literal["INITIAL", "UNCHANGED", "PROGRESSED"]


class WatermarkError(ValueError):
    """Stable fail-closed watermark rejection."""


@dataclass(frozen=True)
class WatermarkResult:
    source: str
    source_identity_hash: str
    previous_sequence: int | None
    current_sequence: int
    delta: int | None
    transition: Transition
    age_seconds: float
    artifact_hash: str


def build_watermark(
    *,
    source: str,
    sequence: int,
    observed_at: datetime,
    source_identity_hash: str,
) -> dict[str, Any]:
    if not source or ":" in source:
        raise WatermarkError("SOURCE_INVALID")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise WatermarkError("SEQUENCE_INVALID")
    _require_aware(observed_at, "OBSERVED_AT_NAIVE")
    _require_sha256(source_identity_hash, "SOURCE_IDENTITY_HASH_INVALID")
    payload: dict[str, Any] = {
        "schema_version": WATERMARK_SCHEMA_VERSION,
        "watermark": f"{source}:{sequence}",
        "source": source,
        "sequence": sequence,
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "source_identity_hash": source_identity_hash,
    }
    payload["artifact_hash"] = _hash(payload)
    return payload


def evaluate_watermark(
    current: Any,
    *,
    previous: Any | None,
    now: datetime,
    max_age_seconds: float,
    max_forward_step: int,
) -> WatermarkResult:
    if max_age_seconds <= 0 or max_forward_step <= 0:
        raise WatermarkError("BOUND_INVALID")
    _require_aware(now, "NOW_NAIVE")
    current_row = _validate(current)
    current_time = _parse_timestamp(current_row["observed_at"])
    age_seconds = (now.astimezone(UTC) - current_time).total_seconds()
    if age_seconds < 0:
        raise WatermarkError("CURRENT_FUTURE_DATED")
    if age_seconds >= max_age_seconds:
        raise WatermarkError("CURRENT_STALE")

    if previous is None:
        return WatermarkResult(
            source=current_row["source"],
            source_identity_hash=current_row["source_identity_hash"],
            previous_sequence=None,
            current_sequence=current_row["sequence"],
            delta=None,
            transition="INITIAL",
            age_seconds=age_seconds,
            artifact_hash=current_row["artifact_hash"],
        )

    previous_row = _validate(previous)
    previous_time = _parse_timestamp(previous_row["observed_at"])
    if previous_time > current_time:
        raise WatermarkError("OBSERVATION_ORDER_INVALID")
    if previous_row["source"] != current_row["source"]:
        raise WatermarkError("SOURCE_CHANGED")
    if previous_row["source_identity_hash"] != current_row["source_identity_hash"]:
        raise WatermarkError("SOURCE_IDENTITY_CHANGED")
    delta = current_row["sequence"] - previous_row["sequence"]
    if delta < 0:
        raise WatermarkError("SEQUENCE_REGRESSED")
    if delta > max_forward_step:
        raise WatermarkError("FORWARD_STEP_EXCEEDED")
    transition: Transition = "UNCHANGED" if delta == 0 else "PROGRESSED"
    return WatermarkResult(
        source=current_row["source"],
        source_identity_hash=current_row["source_identity_hash"],
        previous_sequence=previous_row["sequence"],
        current_sequence=current_row["sequence"],
        delta=delta,
        transition=transition,
        age_seconds=age_seconds,
        artifact_hash=current_row["artifact_hash"],
    )


def _validate(payload: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "watermark",
        "source",
        "sequence",
        "observed_at",
        "source_identity_hash",
        "artifact_hash",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise WatermarkError("WATERMARK_FIELDS_INVALID")
    if payload["schema_version"] != WATERMARK_SCHEMA_VERSION:
        raise WatermarkError("WATERMARK_SCHEMA_UNSUPPORTED")
    if not isinstance(payload["source"], str) or not payload["source"] or ":" in payload["source"]:
        raise WatermarkError("SOURCE_INVALID")
    sequence = payload["sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise WatermarkError("SEQUENCE_INVALID")
    if payload["watermark"] != f"{payload['source']}:{sequence}":
        raise WatermarkError("WATERMARK_CANONICAL_FORM_INVALID")
    _parse_timestamp(payload["observed_at"])
    _require_sha256(payload["source_identity_hash"], "SOURCE_IDENTITY_HASH_INVALID")
    unhashed = {key: value for key, value in payload.items() if key != "artifact_hash"}
    if payload["artifact_hash"] != _hash(unhashed):
        raise WatermarkError("ARTIFACT_HASH_MISMATCH")
    return payload


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise WatermarkError("OBSERVED_AT_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WatermarkError("OBSERVED_AT_INVALID") from exc
    _require_aware(parsed, "OBSERVED_AT_NAIVE")
    return parsed.astimezone(UTC)


def _require_aware(value: datetime, reason: str) -> None:
    if value.tzinfo is None:
        raise WatermarkError(reason)


def _require_sha256(value: Any, reason: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise WatermarkError(reason)
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise WatermarkError(reason) from exc


def _hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
