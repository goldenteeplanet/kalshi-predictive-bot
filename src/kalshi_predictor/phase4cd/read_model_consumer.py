from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_SCHEMA_VERSION = "phase4fm-evidence-read-model-v1"


class ReadModelContractError(ValueError):
    """A stable, fail-closed consumer-contract rejection."""


@dataclass(frozen=True)
class ReadModelConsumerContract:
    schema_version: str = DEFAULT_SCHEMA_VERSION
    max_age_seconds: float = 300.0
    max_artifact_bytes: int = 1_048_576
    watermark_prefix: str = "paper_pnl:"
    required_lane: str = "GUARDED_PAPER"

    def __post_init__(self) -> None:
        if not self.schema_version:
            raise ReadModelContractError("CONTRACT_SCHEMA_EMPTY")
        if self.max_age_seconds <= 0:
            raise ReadModelContractError("CONTRACT_MAX_AGE_INVALID")
        if self.max_artifact_bytes <= 0:
            raise ReadModelContractError("CONTRACT_MAX_BYTES_INVALID")
        if not self.watermark_prefix or not self.required_lane:
            raise ReadModelContractError("CONTRACT_REQUIREMENT_EMPTY")


@dataclass(frozen=True)
class ReadModelView:
    schema_version: str
    generated_at: str
    source_watermark: str
    source_database_identity_hash: str
    age_seconds: float
    guarded_paper_settled: int
    realized_pnl: str
    paper_order_count: int
    evidence_lane: Mapping[str, Any]
    payload_hash: str
    manifest_hash: str


def consume_path(
    path: Path,
    *,
    contract: ReadModelConsumerContract,
    now: datetime,
) -> ReadModelView:
    """Read one bounded artifact without opening a database or invoking a writer."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReadModelContractError("ARTIFACT_UNAVAILABLE") from exc
    if size == 0:
        raise ReadModelContractError("ARTIFACT_EMPTY")
    if size > contract.max_artifact_bytes:
        raise ReadModelContractError("ARTIFACT_SIZE_EXCEEDED")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReadModelContractError("ARTIFACT_UNAVAILABLE") from exc
    return consume_bytes(raw, contract=contract, now=now)


def consume_bytes(
    raw: bytes,
    *,
    contract: ReadModelConsumerContract,
    now: datetime,
) -> ReadModelView:
    if not raw:
        raise ReadModelContractError("ARTIFACT_EMPTY")
    if len(raw) > contract.max_artifact_bytes:
        raise ReadModelContractError("ARTIFACT_SIZE_EXCEEDED")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadModelContractError("ARTIFACT_MALFORMED") from exc
    return consume_payload(payload, contract=contract, now=now)


def consume_payload(
    payload: Any,
    *,
    contract: ReadModelConsumerContract,
    now: datetime,
) -> ReadModelView:
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
    if not isinstance(payload, dict) or set(payload) != required:
        raise ReadModelContractError("ARTIFACT_FIELDS_INVALID")
    if payload["schema_version"] != contract.schema_version:
        raise ReadModelContractError("SCHEMA_INCOMPATIBLE")
    if not isinstance(payload["source_watermark"], str) or not payload[
        "source_watermark"
    ].startswith(contract.watermark_prefix):
        raise ReadModelContractError("WATERMARK_INCOMPATIBLE")
    _require_sha256(payload["source_database_identity_hash"], "PROVENANCE_HASH_INVALID")
    previous = payload["previous_artifact_hash"]
    if previous is not None:
        _require_sha256(previous, "PREVIOUS_HASH_INVALID")
    if not isinstance(payload["evidence_lanes"], dict):
        raise ReadModelContractError("EVIDENCE_LANES_INVALID")
    lane = payload["evidence_lanes"].get(contract.required_lane)
    if not isinstance(lane, dict):
        raise ReadModelContractError("REQUIRED_LANE_MISSING")
    for key in ("guarded_paper_settled", "paper_order_count"):
        if not isinstance(payload[key], int) or isinstance(payload[key], bool) or payload[key] < 0:
            raise ReadModelContractError("COUNT_INVALID")
    if not isinstance(payload["realized_pnl"], str):
        raise ReadModelContractError("REALIZED_PNL_INVALID")

    unhashed = {
        key: value for key, value in payload.items() if key not in {"payload_hash", "manifest_hash"}
    }
    expected_payload_hash = _hash(unhashed)
    if payload["payload_hash"] != expected_payload_hash:
        raise ReadModelContractError("PAYLOAD_HASH_MISMATCH")
    expected_manifest_hash = _hash(
        {
            "schema_version": contract.schema_version,
            "payload_hash": expected_payload_hash,
            "previous_artifact_hash": previous,
            "source_watermark": payload["source_watermark"],
        }
    )
    if payload["manifest_hash"] != expected_manifest_hash:
        raise ReadModelContractError("MANIFEST_HASH_MISMATCH")

    generated_at = _parse_timestamp(payload["generated_at"])
    if now.tzinfo is None:
        raise ReadModelContractError("NOW_TIMESTAMP_NAIVE")
    age_seconds = (now.astimezone(UTC) - generated_at).total_seconds()
    if age_seconds < 0:
        raise ReadModelContractError("ARTIFACT_FUTURE_DATED")
    if age_seconds >= contract.max_age_seconds:
        raise ReadModelContractError("ARTIFACT_STALE")
    return ReadModelView(
        schema_version=payload["schema_version"],
        generated_at=payload["generated_at"],
        source_watermark=payload["source_watermark"],
        source_database_identity_hash=payload["source_database_identity_hash"],
        age_seconds=age_seconds,
        guarded_paper_settled=payload["guarded_paper_settled"],
        realized_pnl=payload["realized_pnl"],
        paper_order_count=payload["paper_order_count"],
        evidence_lane=lane,
        payload_hash=payload["payload_hash"],
        manifest_hash=payload["manifest_hash"],
    )


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ReadModelContractError("TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReadModelContractError("TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ReadModelContractError("TIMESTAMP_NAIVE")
    return parsed.astimezone(UTC)


def _require_sha256(value: Any, reason: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ReadModelContractError(reason)
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ReadModelContractError(reason) from exc


def _hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
