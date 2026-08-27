from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

MATRIX_SCHEMA_VERSION = "phase4fo-read-model-compatibility-matrix-v1"
Decision = Literal["COMPATIBLE", "INCOMPATIBLE"]


class CompatibilityMatrixError(ValueError):
    """Stable fail-closed compatibility-matrix rejection."""


@dataclass(frozen=True)
class CompatibilityResult:
    producer_schema: str
    consumer_schema: str
    decision: Decision
    reason: str
    matrix_hash: str
    age_seconds: float


def build_matrix(
    *,
    generated_at: datetime,
    entries: list[dict[str, str]],
    provenance_hash: str,
) -> dict[str, Any]:
    _require_aware(generated_at, "GENERATED_AT_NAIVE")
    normalized = [_normalize_entry(entry) for entry in entries]
    _validate_entry_order(normalized)
    _require_sha256(provenance_hash, "PROVENANCE_HASH_INVALID")
    payload: dict[str, Any] = {
        "schema_version": MATRIX_SCHEMA_VERSION,
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "provenance_hash": provenance_hash,
        "entries": normalized,
    }
    payload["matrix_hash"] = _hash(payload)
    return payload


def assess_compatibility(
    matrix: Any,
    *,
    producer_schema: str,
    consumer_schema: str,
    now: datetime,
    max_age_seconds: float,
    max_entries: int = 64,
) -> CompatibilityResult:
    if max_age_seconds <= 0 or max_entries <= 0:
        raise CompatibilityMatrixError("CONSUMER_BOUND_INVALID")
    _require_aware(now, "NOW_NAIVE")
    required = {"schema_version", "generated_at", "provenance_hash", "entries", "matrix_hash"}
    if not isinstance(matrix, dict) or set(matrix) != required:
        raise CompatibilityMatrixError("MATRIX_FIELDS_INVALID")
    if matrix["schema_version"] != MATRIX_SCHEMA_VERSION:
        raise CompatibilityMatrixError("MATRIX_SCHEMA_UNSUPPORTED")
    _require_sha256(matrix["provenance_hash"], "PROVENANCE_HASH_INVALID")
    if not isinstance(matrix["entries"], list):
        raise CompatibilityMatrixError("MATRIX_ENTRIES_INVALID")
    if not matrix["entries"]:
        raise CompatibilityMatrixError("MATRIX_EMPTY")
    if len(matrix["entries"]) > max_entries:
        raise CompatibilityMatrixError("MATRIX_ENTRY_BOUND_EXCEEDED")
    entries = [_normalize_entry(entry) for entry in matrix["entries"]]
    _validate_entry_order(entries)
    unhashed = {key: value for key, value in matrix.items() if key != "matrix_hash"}
    if matrix["matrix_hash"] != _hash(unhashed):
        raise CompatibilityMatrixError("MATRIX_HASH_MISMATCH")

    generated_at = _parse_timestamp(matrix["generated_at"])
    age_seconds = (now.astimezone(UTC) - generated_at).total_seconds()
    if age_seconds < 0:
        raise CompatibilityMatrixError("MATRIX_FUTURE_DATED")
    if age_seconds >= max_age_seconds:
        raise CompatibilityMatrixError("MATRIX_STALE")
    if not producer_schema or not consumer_schema:
        raise CompatibilityMatrixError("SCHEMA_IDENTITY_EMPTY")

    for entry in entries:
        if (
            entry["producer_schema"] == producer_schema
            and entry["consumer_schema"] == consumer_schema
        ):
            return CompatibilityResult(
                producer_schema=producer_schema,
                consumer_schema=consumer_schema,
                decision=cast(Decision, entry["decision"]),
                reason=entry["reason"],
                matrix_hash=matrix["matrix_hash"],
                age_seconds=age_seconds,
            )
    raise CompatibilityMatrixError("SCHEMA_PAIR_UNDECLARED")


def _normalize_entry(entry: Any) -> dict[str, str]:
    required = {"producer_schema", "consumer_schema", "decision", "reason"}
    if not isinstance(entry, dict) or set(entry) != required:
        raise CompatibilityMatrixError("ENTRY_FIELDS_INVALID")
    if not all(isinstance(entry[key], str) and entry[key] for key in required):
        raise CompatibilityMatrixError("ENTRY_VALUE_INVALID")
    if entry["decision"] not in {"COMPATIBLE", "INCOMPATIBLE"}:
        raise CompatibilityMatrixError("ENTRY_DECISION_INVALID")
    return {key: entry[key] for key in sorted(required)}


def _validate_entry_order(entries: list[dict[str, str]]) -> None:
    keys = [(entry["producer_schema"], entry["consumer_schema"]) for entry in entries]
    if len(keys) != len(set(keys)):
        raise CompatibilityMatrixError("ENTRY_DUPLICATE")
    if keys != sorted(keys):
        raise CompatibilityMatrixError("ENTRY_ORDER_INVALID")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CompatibilityMatrixError("GENERATED_AT_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CompatibilityMatrixError("GENERATED_AT_INVALID") from exc
    _require_aware(parsed, "GENERATED_AT_NAIVE")
    return parsed.astimezone(UTC)


def _require_aware(value: datetime, reason: str) -> None:
    if value.tzinfo is None:
        raise CompatibilityMatrixError(reason)


def _require_sha256(value: Any, reason: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise CompatibilityMatrixError(reason)
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise CompatibilityMatrixError(reason) from exc


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
