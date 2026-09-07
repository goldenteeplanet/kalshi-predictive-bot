from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "phase4fm-evidence-read-model-v1"


class EvidenceReadModelError(ValueError):
    pass


def database_identity_hash(*, backend: str, stable_identity: str) -> str:
    """Hash a caller-supplied non-secret database identity."""
    return hashlib.sha256(f"{backend}:{stable_identity}".encode()).hexdigest()


def build_read_model(
    *,
    generated_at: datetime,
    source_database_identity_hash: str,
    source_watermark: str,
    guarded_paper_settled: int,
    realized_pnl: str,
    paper_order_count: int,
    evidence_lanes: dict[str, dict[str, Any]],
    previous_artifact_hash: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_iso(generated_at),
        "source_database_identity_hash": source_database_identity_hash,
        "source_watermark": source_watermark,
        "guarded_paper_settled": guarded_paper_settled,
        "realized_pnl": realized_pnl,
        "paper_order_count": paper_order_count,
        "evidence_lanes": evidence_lanes,
        "previous_artifact_hash": previous_artifact_hash,
    }
    payload["payload_hash"] = _hash(payload)
    payload["manifest_hash"] = _hash(
        {
            "schema_version": SCHEMA_VERSION,
            "payload_hash": payload["payload_hash"],
            "previous_artifact_hash": previous_artifact_hash,
            "source_watermark": source_watermark,
        }
    )
    return payload


def publish_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Offline/test publisher. The UI never calls this function."""
    validate_read_model(payload, now=datetime.now(UTC), max_age_seconds=None)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_read_model(
    path: Path,
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceReadModelError("READ_MODEL_UNAVAILABLE_OR_MALFORMED") from exc
    validate_read_model(payload, now=now, max_age_seconds=max_age_seconds)
    return payload


def load_ui_read_model(
    path: Path | None,
    *,
    now: datetime,
    max_age_seconds: float,
    allow_database_fallback: bool,
) -> dict[str, Any] | None:
    """Optional UI adapter; disabled when no path is explicitly configured."""
    if path is None:
        return None
    try:
        payload = load_read_model(path, now=now, max_age_seconds=max_age_seconds)
    except EvidenceReadModelError as exc:
        if not allow_database_fallback:
            raise
        return {
            "read_model_source": "DATABASE_FALLBACK_REQUIRED",
            "fallback_used": True,
            "fallback_reason": str(exc),
        }
    generated_at = datetime.fromisoformat(payload["generated_at"])
    return {
        "read_model_source": "HASH_PROTECTED_ARTIFACT",
        "source_watermark": payload["source_watermark"],
        "source_age_seconds": round(
            (now.astimezone(UTC) - generated_at.astimezone(UTC)).total_seconds(),
            3,
        ),
        "fallback_used": False,
        "fallback_reason": None,
        "guarded_paper_settled": payload["guarded_paper_settled"],
        "realized_pnl": payload["realized_pnl"],
        "paper_order_count": payload["paper_order_count"],
        "evidence_lanes": payload["evidence_lanes"],
        "payload_hash": payload["payload_hash"],
    }


def validate_read_model(
    payload: Any,
    *,
    now: datetime,
    max_age_seconds: float | None,
) -> None:
    required = {
        "schema_version",
        "generated_at",
        "source_database_identity_hash",
        "source_watermark",
        "guarded_paper_settled",
        "realized_pnl",
        "paper_order_count",
        "evidence_lanes",
        "previous_artifact_hash",
        "payload_hash",
        "manifest_hash",
    }
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise EvidenceReadModelError("READ_MODEL_REQUIRED_FIELD_MISSING")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise EvidenceReadModelError("READ_MODEL_SCHEMA_UNSUPPORTED")
    if not payload["source_watermark"]:
        raise EvidenceReadModelError("READ_MODEL_WATERMARK_MISSING")
    unhashed = {
        key: value
        for key, value in payload.items()
        if key not in {"payload_hash", "manifest_hash"}
    }
    if payload["payload_hash"] != _hash(unhashed):
        raise EvidenceReadModelError("READ_MODEL_PAYLOAD_HASH_MISMATCH")
    expected_manifest = _hash(
        {
            "schema_version": SCHEMA_VERSION,
            "payload_hash": payload["payload_hash"],
            "previous_artifact_hash": payload["previous_artifact_hash"],
            "source_watermark": payload["source_watermark"],
        }
    )
    if payload["manifest_hash"] != expected_manifest:
        raise EvidenceReadModelError("READ_MODEL_MANIFEST_HASH_MISMATCH")
    generated_at = datetime.fromisoformat(payload["generated_at"])
    if generated_at.tzinfo is None:
        raise EvidenceReadModelError("READ_MODEL_TIMESTAMP_NAIVE")
    age = (now.astimezone(UTC) - generated_at.astimezone(UTC)).total_seconds()
    if age < 0:
        raise EvidenceReadModelError("READ_MODEL_FUTURE_DATED")
    if max_age_seconds is not None and age >= max_age_seconds:
        raise EvidenceReadModelError("READ_MODEL_STALE")


def _hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise EvidenceReadModelError("READ_MODEL_TIMESTAMP_NAIVE")
    return value.astimezone(UTC).isoformat()
