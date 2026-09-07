from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.evidence_cache_memory_bounds import (
    CacheEntryDescriptor,
    EvidenceCacheMemoryBounds,
    validate_evidence_cache_memory_bounds,
)

INTEGRITY_SCHEMA_VERSION = "phase4gi-evidence-cache-provenance-integrity-v1"
IntegrityStatus = Literal["INTACT", "STALE"]


class EvidenceCacheProvenanceIntegrityError(ValueError):
    """Stable fail-closed cache-provenance integrity error."""


@dataclass(frozen=True)
class CacheProvenanceRecord:
    descriptor: CacheEntryDescriptor
    content_hash: str
    previous_record_hash: str | None
    record_hash: str


@dataclass(frozen=True)
class EvidenceCacheProvenanceIntegrity:
    status: IntegrityStatus
    reasons: tuple[str, ...]
    memory_bounds_hash: str
    source_identity_hash: str
    source_watermark: str
    record_count: int
    genesis_record_hash: str
    head_record_hash: str
    entries_hash: str
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    integrity_hash: str
    execution_authorized: bool = False


def make_cache_provenance_record(
    *,
    descriptor: Any,
    content_hash: str,
    previous_record_hash: str | None,
) -> CacheProvenanceRecord:
    validated = _validated_descriptor(descriptor)
    if not isinstance(content_hash, str) or not content_hash:
        raise EvidenceCacheProvenanceIntegrityError("CONTENT_HASH_INVALID")
    if previous_record_hash is not None and (
        not isinstance(previous_record_hash, str) or not previous_record_hash
    ):
        raise EvidenceCacheProvenanceIntegrityError("PREVIOUS_HASH_INVALID")
    unsigned = {
        "descriptor": asdict(validated),
        "content_hash": content_hash,
        "previous_record_hash": previous_record_hash,
    }
    return CacheProvenanceRecord(
        descriptor=validated,
        content_hash=content_hash,
        previous_record_hash=previous_record_hash,
        record_hash=_hash(unsigned),
    )


def verify_cache_provenance_integrity(
    *,
    memory_bounds: Any,
    records: Sequence[Any],
    max_records: int = 256,
    max_evidence_age_seconds: int = 300,
) -> EvidenceCacheProvenanceIntegrity:
    if max_records <= 0 or max_evidence_age_seconds < 0:
        raise EvidenceCacheProvenanceIntegrityError("INTEGRITY_BOUND_INVALID")
    if not records:
        raise EvidenceCacheProvenanceIntegrityError("RECORDS_EMPTY")
    if len(records) > max_records:
        raise EvidenceCacheProvenanceIntegrityError("RECORD_BOUND_EXCEEDED")
    try:
        validate_evidence_cache_memory_bounds(memory_bounds)
    except (TypeError, ValueError) as exc:
        raise EvidenceCacheProvenanceIntegrityError("MEMORY_BOUNDS_INPUT_INVALID") from exc
    if not isinstance(memory_bounds, EvidenceCacheMemoryBounds):
        raise EvidenceCacheProvenanceIntegrityError("MEMORY_BOUNDS_INPUT_INVALID")
    if len(records) != memory_bounds.entry_count:
        raise EvidenceCacheProvenanceIntegrityError("RECORD_COUNT_MISMATCH")
    validated = [_validated_record(record) for record in records]
    expected_previous: str | None = None
    for record in validated:
        if record.previous_record_hash != expected_previous:
            raise EvidenceCacheProvenanceIntegrityError("PROVENANCE_CHAIN_BROKEN")
        expected_previous = record.record_hash
    if any(
        record.descriptor.source_identity_hash != memory_bounds.source_identity_hash
        for record in validated
    ):
        raise EvidenceCacheProvenanceIntegrityError("SOURCE_IDENTITY_MISMATCH")
    if any(
        record.descriptor.source_watermark != memory_bounds.source_watermark for record in validated
    ):
        raise EvidenceCacheProvenanceIntegrityError("SOURCE_WATERMARK_MISMATCH")
    entries_hash = _hash([asdict(record.descriptor) for record in validated])
    if entries_hash != memory_bounds.entries_hash:
        raise EvidenceCacheProvenanceIntegrityError("ENTRY_MANIFEST_MISMATCH")

    observed_age = max(record.descriptor.age_seconds for record in validated)
    reasons: list[str] = []
    if memory_bounds.status == "STALE" or observed_age > max_evidence_age_seconds:
        status: IntegrityStatus = "STALE"
        reasons.append("PROVENANCE_EVIDENCE_STALE")
    else:
        status = "INTACT"
    unsigned = {
        "schema_version": INTEGRITY_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "memory_bounds_hash": memory_bounds.bounds_hash,
        "source_identity_hash": memory_bounds.source_identity_hash,
        "source_watermark": memory_bounds.source_watermark,
        "record_count": len(validated),
        "genesis_record_hash": validated[0].record_hash,
        "head_record_hash": validated[-1].record_hash,
        "entries_hash": entries_hash,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "execution_authorized": False,
    }
    return EvidenceCacheProvenanceIntegrity(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        memory_bounds_hash=memory_bounds.bounds_hash,
        source_identity_hash=memory_bounds.source_identity_hash,
        source_watermark=memory_bounds.source_watermark,
        record_count=len(validated),
        genesis_record_hash=validated[0].record_hash,
        head_record_hash=validated[-1].record_hash,
        entries_hash=entries_hash,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        integrity_hash=_hash(unsigned),
    )


def validate_cache_provenance_integrity(integrity: Any) -> None:
    if not isinstance(integrity, EvidenceCacheProvenanceIntegrity):
        raise EvidenceCacheProvenanceIntegrityError("INTEGRITY_RESULT_TYPE_INVALID")
    if integrity.execution_authorized is not False:
        raise EvidenceCacheProvenanceIntegrityError("INTEGRITY_SAFETY_BOUNDARY_INVALID")
    if integrity.status == "INTACT" and integrity.reasons:
        raise EvidenceCacheProvenanceIntegrityError("INTACT_REASONS_INVALID")
    if integrity.status == "STALE" and not integrity.reasons:
        raise EvidenceCacheProvenanceIntegrityError("STALE_REASONS_MISSING")
    unsigned = asdict(integrity)
    unsigned.pop("integrity_hash")
    unsigned["schema_version"] = INTEGRITY_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if integrity.integrity_hash != _hash(unsigned):
        raise EvidenceCacheProvenanceIntegrityError("INTEGRITY_HASH_MISMATCH")


def _validated_record(value: Any) -> CacheProvenanceRecord:
    if not isinstance(value, CacheProvenanceRecord):
        raise EvidenceCacheProvenanceIntegrityError("RECORD_TYPE_INVALID")
    descriptor = _validated_descriptor(value.descriptor)
    unsigned = {
        "descriptor": asdict(descriptor),
        "content_hash": value.content_hash,
        "previous_record_hash": value.previous_record_hash,
    }
    if value.record_hash != _hash(unsigned):
        raise EvidenceCacheProvenanceIntegrityError("RECORD_HASH_MISMATCH")
    return value


def _validated_descriptor(value: Any) -> CacheEntryDescriptor:
    if not isinstance(value, CacheEntryDescriptor):
        raise EvidenceCacheProvenanceIntegrityError("DESCRIPTOR_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("descriptor_hash")
    if supplied_hash != _hash(unsigned):
        raise EvidenceCacheProvenanceIntegrityError("DESCRIPTOR_HASH_MISMATCH")
    return value


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
