from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.evidence_cache_single_flight_proposal import (
    EvidenceCacheSingleFlightProposal,
    validate_single_flight_proposal,
)

MEMORY_SCHEMA_VERSION = "phase4gh-evidence-cache-memory-bounds-v1"
MemoryStatus = Literal["WITHIN_BOUNDS", "EVICTION_REQUIRED", "STALE"]


class EvidenceCacheMemoryBoundsError(ValueError):
    """Stable fail-closed cache-memory bounds error."""


@dataclass(frozen=True)
class CacheEntryDescriptor:
    cache_key_hash: str
    source_identity_hash: str
    source_watermark: str
    size_bytes: int
    age_seconds: int
    descriptor_hash: str


@dataclass(frozen=True)
class EvidenceCacheMemoryBounds:
    status: MemoryStatus
    reasons: tuple[str, ...]
    proposal_hash: str
    source_identity_hash: str
    source_watermark: str
    entry_count: int
    total_bytes: int
    max_entries: int
    max_total_bytes: int
    max_entry_bytes: int
    max_evidence_age_seconds: int
    observed_max_age_seconds: int
    eviction_key_hashes: tuple[str, ...]
    entries_hash: str
    bounds_hash: str
    execution_authorized: bool = False


def make_cache_entry_descriptor(
    *,
    cache_key_hash: str,
    source_identity_hash: str,
    source_watermark: str,
    size_bytes: int,
    age_seconds: int,
) -> CacheEntryDescriptor:
    unsigned = {
        "cache_key_hash": cache_key_hash,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "size_bytes": size_bytes,
        "age_seconds": age_seconds,
    }
    _validate_descriptor_fields(unsigned)
    return CacheEntryDescriptor(**unsigned, descriptor_hash=_hash(unsigned))


def evaluate_evidence_cache_memory_bounds(
    *,
    proposal: Any,
    entries: Sequence[Any],
    max_input_entries: int = 256,
    max_entries: int = 64,
    max_total_bytes: int = 1_048_576,
    max_entry_bytes: int = 65_536,
    max_evidence_age_seconds: int = 300,
) -> EvidenceCacheMemoryBounds:
    for value in (max_input_entries, max_entries, max_total_bytes, max_entry_bytes):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EvidenceCacheMemoryBoundsError("MEMORY_BOUND_INVALID")
    if max_evidence_age_seconds < 0:
        raise EvidenceCacheMemoryBoundsError("MEMORY_BOUND_INVALID")
    if not entries:
        raise EvidenceCacheMemoryBoundsError("ENTRIES_EMPTY")
    if len(entries) > max_input_entries:
        raise EvidenceCacheMemoryBoundsError("INPUT_ENTRY_BOUND_EXCEEDED")
    try:
        validate_single_flight_proposal(proposal)
    except (TypeError, ValueError) as exc:
        raise EvidenceCacheMemoryBoundsError("PROPOSAL_INPUT_INVALID") from exc
    if not isinstance(proposal, EvidenceCacheSingleFlightProposal):
        raise EvidenceCacheMemoryBoundsError("PROPOSAL_INPUT_INVALID")
    if proposal.decision != "READY":
        raise EvidenceCacheMemoryBoundsError("PROPOSAL_NOT_READY")
    validated = [_validated_descriptor(item) for item in entries]
    keys = [item.cache_key_hash for item in validated]
    if len(set(keys)) != len(keys):
        raise EvidenceCacheMemoryBoundsError("ENTRY_KEY_DUPLICATE")
    if any(item.source_identity_hash != proposal.source_identity_hash for item in validated):
        raise EvidenceCacheMemoryBoundsError("SOURCE_IDENTITY_MISMATCH")
    if any(item.source_watermark != proposal.source_watermark for item in validated):
        raise EvidenceCacheMemoryBoundsError("SOURCE_WATERMARK_MISMATCH")

    total_bytes = sum(item.size_bytes for item in validated)
    observed_age = max(item.age_seconds for item in validated)
    reasons: list[str] = []
    eviction: tuple[str, ...] = ()
    if observed_age > max_evidence_age_seconds:
        status: MemoryStatus = "STALE"
        reasons.append("MEMORY_EVIDENCE_STALE")
    else:
        if len(validated) > max_entries:
            reasons.append("ENTRY_COUNT_EXCEEDED")
        if total_bytes > max_total_bytes:
            reasons.append("TOTAL_BYTES_EXCEEDED")
        if any(item.size_bytes > max_entry_bytes for item in validated):
            reasons.append("ENTRY_BYTES_EXCEEDED")
        status = "EVICTION_REQUIRED" if reasons else "WITHIN_BOUNDS"
        if reasons:
            eviction = _eviction_plan(
                validated,
                max_entries=max_entries,
                max_total_bytes=max_total_bytes,
                max_entry_bytes=max_entry_bytes,
            )
    entries_hash = _hash([asdict(item) for item in validated])
    unsigned = {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "proposal_hash": proposal.proposal_hash,
        "source_identity_hash": proposal.source_identity_hash,
        "source_watermark": proposal.source_watermark,
        "entry_count": len(validated),
        "total_bytes": total_bytes,
        "max_entries": max_entries,
        "max_total_bytes": max_total_bytes,
        "max_entry_bytes": max_entry_bytes,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "observed_max_age_seconds": observed_age,
        "eviction_key_hashes": list(eviction),
        "entries_hash": entries_hash,
        "execution_authorized": False,
    }
    return EvidenceCacheMemoryBounds(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        proposal_hash=proposal.proposal_hash,
        source_identity_hash=proposal.source_identity_hash,
        source_watermark=proposal.source_watermark,
        entry_count=len(validated),
        total_bytes=total_bytes,
        max_entries=max_entries,
        max_total_bytes=max_total_bytes,
        max_entry_bytes=max_entry_bytes,
        max_evidence_age_seconds=max_evidence_age_seconds,
        observed_max_age_seconds=observed_age,
        eviction_key_hashes=eviction,
        entries_hash=entries_hash,
        bounds_hash=_hash(unsigned),
    )


def validate_evidence_cache_memory_bounds(bounds: Any) -> None:
    if not isinstance(bounds, EvidenceCacheMemoryBounds):
        raise EvidenceCacheMemoryBoundsError("MEMORY_RESULT_TYPE_INVALID")
    if bounds.execution_authorized is not False:
        raise EvidenceCacheMemoryBoundsError("MEMORY_SAFETY_BOUNDARY_INVALID")
    if bounds.status == "WITHIN_BOUNDS" and (bounds.reasons or bounds.eviction_key_hashes):
        raise EvidenceCacheMemoryBoundsError("WITHIN_BOUNDS_STATE_INVALID")
    if bounds.status == "EVICTION_REQUIRED" and not bounds.eviction_key_hashes:
        raise EvidenceCacheMemoryBoundsError("EVICTION_PLAN_MISSING")
    if bounds.status == "STALE" and bounds.eviction_key_hashes:
        raise EvidenceCacheMemoryBoundsError("STALE_EVICTION_INVALID")
    unsigned = asdict(bounds)
    unsigned.pop("bounds_hash")
    unsigned["schema_version"] = MEMORY_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["eviction_key_hashes"] = list(unsigned["eviction_key_hashes"])
    if bounds.bounds_hash != _hash(unsigned):
        raise EvidenceCacheMemoryBoundsError("MEMORY_HASH_MISMATCH")


def _eviction_plan(
    entries: list[CacheEntryDescriptor],
    *,
    max_entries: int,
    max_total_bytes: int,
    max_entry_bytes: int,
) -> tuple[str, ...]:
    ordered = sorted(
        entries,
        key=lambda item: (
            item.size_bytes <= max_entry_bytes,
            -item.age_seconds,
            -item.size_bytes,
            item.cache_key_hash,
        ),
    )
    remaining_count = len(entries)
    remaining_bytes = sum(item.size_bytes for item in entries)
    evicted: list[str] = []
    for item in ordered:
        if (
            remaining_count <= max_entries
            and remaining_bytes <= max_total_bytes
            and item.size_bytes <= max_entry_bytes
        ):
            continue
        evicted.append(item.cache_key_hash)
        remaining_count -= 1
        remaining_bytes -= item.size_bytes
    return tuple(evicted)


def _validated_descriptor(value: Any) -> CacheEntryDescriptor:
    if not isinstance(value, CacheEntryDescriptor):
        raise EvidenceCacheMemoryBoundsError("ENTRY_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("descriptor_hash")
    _validate_descriptor_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise EvidenceCacheMemoryBoundsError("ENTRY_HASH_MISMATCH")
    return value


def _validate_descriptor_fields(payload: dict[str, Any]) -> None:
    for key in ("cache_key_hash", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise EvidenceCacheMemoryBoundsError("ENTRY_FIELD_INVALID")
    for key in ("size_bytes", "age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvidenceCacheMemoryBoundsError("ENTRY_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
