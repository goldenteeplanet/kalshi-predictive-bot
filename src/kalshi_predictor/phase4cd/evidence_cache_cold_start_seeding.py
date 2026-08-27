from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.evidence_cache_provenance_integrity import (
    EvidenceCacheProvenanceIntegrity,
    validate_cache_provenance_integrity,
)

SEED_SCHEMA_VERSION = "phase4gj-evidence-cache-cold-start-seeding-proposal-v1"
SeedDecision = Literal["PROPOSE", "REJECT"]


class EvidenceCacheColdStartSeedError(ValueError):
    """Stable fail-closed cold-start seed proposal error."""


@dataclass(frozen=True)
class CacheSeedCandidate:
    cache_key_hash: str
    content_hash: str
    source_identity_hash: str
    source_watermark: str
    evidence_age_seconds: int
    size_bytes: int
    priority: int
    candidate_hash: str


@dataclass(frozen=True)
class CacheSeedItem:
    cache_key_hash: str
    content_hash: str
    size_bytes: int
    priority: int


@dataclass(frozen=True)
class EvidenceCacheColdStartSeedProposal:
    decision: SeedDecision
    reasons: tuple[str, ...]
    integrity_hash: str
    source_identity_hash: str
    source_watermark: str
    candidate_count: int
    selected_count: int
    omitted_count: int
    selected_total_bytes: int
    max_seed_entries: int
    max_seed_bytes: int
    max_evidence_age_seconds: int
    selected_items: tuple[CacheSeedItem, ...]
    candidates_hash: str
    proposal_hash: str
    execution_authorized: bool = False


def make_cache_seed_candidate(
    *,
    cache_key_hash: str,
    content_hash: str,
    source_identity_hash: str,
    source_watermark: str,
    evidence_age_seconds: int,
    size_bytes: int,
    priority: int,
) -> CacheSeedCandidate:
    unsigned = {
        "cache_key_hash": cache_key_hash,
        "content_hash": content_hash,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "evidence_age_seconds": evidence_age_seconds,
        "size_bytes": size_bytes,
        "priority": priority,
    }
    _validate_candidate_fields(unsigned)
    return CacheSeedCandidate(**unsigned, candidate_hash=_hash(unsigned))


def propose_cache_cold_start_seed(
    *,
    integrity: Any,
    candidates: Sequence[Any],
    max_candidates: int = 128,
    max_seed_entries: int = 8,
    max_seed_bytes: int = 262_144,
    max_evidence_age_seconds: int = 300,
) -> EvidenceCacheColdStartSeedProposal:
    for value in (max_candidates, max_seed_entries, max_seed_bytes):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EvidenceCacheColdStartSeedError("SEED_BOUND_INVALID")
    if max_evidence_age_seconds < 0:
        raise EvidenceCacheColdStartSeedError("SEED_BOUND_INVALID")
    if not candidates:
        raise EvidenceCacheColdStartSeedError("CANDIDATES_EMPTY")
    if len(candidates) > max_candidates:
        raise EvidenceCacheColdStartSeedError("CANDIDATE_BOUND_EXCEEDED")
    try:
        validate_cache_provenance_integrity(integrity)
    except (TypeError, ValueError) as exc:
        raise EvidenceCacheColdStartSeedError("INTEGRITY_INPUT_INVALID") from exc
    if not isinstance(integrity, EvidenceCacheProvenanceIntegrity):
        raise EvidenceCacheColdStartSeedError("INTEGRITY_INPUT_INVALID")
    validated = [_validated_candidate(item) for item in candidates]
    keys = [item.cache_key_hash for item in validated]
    if len(set(keys)) != len(keys):
        raise EvidenceCacheColdStartSeedError("CANDIDATE_KEY_DUPLICATE")
    if any(item.source_identity_hash != integrity.source_identity_hash for item in validated):
        raise EvidenceCacheColdStartSeedError("SOURCE_IDENTITY_MISMATCH")
    if any(item.source_watermark != integrity.source_watermark for item in validated):
        raise EvidenceCacheColdStartSeedError("SOURCE_WATERMARK_MISMATCH")

    reasons: list[str] = []
    selected: list[CacheSeedItem] = []
    selected_bytes = 0
    stale = integrity.status != "INTACT" or any(
        item.evidence_age_seconds > max_evidence_age_seconds for item in validated
    )
    if stale:
        reasons.append("SEED_EVIDENCE_STALE")
    else:
        ordered = sorted(
            validated,
            key=lambda item: (-item.priority, item.evidence_age_seconds, item.cache_key_hash),
        )
        for item in ordered:
            if len(selected) >= max_seed_entries:
                continue
            if selected_bytes + item.size_bytes > max_seed_bytes:
                continue
            selected.append(
                CacheSeedItem(
                    cache_key_hash=item.cache_key_hash,
                    content_hash=item.content_hash,
                    size_bytes=item.size_bytes,
                    priority=item.priority,
                )
            )
            selected_bytes += item.size_bytes
        if not selected:
            reasons.append("NO_SEED_CANDIDATE_FITS")
    decision: SeedDecision = "PROPOSE" if not reasons else "REJECT"
    candidates_hash = _hash([asdict(item) for item in validated])
    unsigned = {
        "schema_version": SEED_SCHEMA_VERSION,
        "decision": decision,
        "reasons": sorted(reasons),
        "integrity_hash": integrity.integrity_hash,
        "source_identity_hash": integrity.source_identity_hash,
        "source_watermark": integrity.source_watermark,
        "candidate_count": len(validated),
        "selected_count": len(selected),
        "omitted_count": len(validated) - len(selected),
        "selected_total_bytes": selected_bytes,
        "max_seed_entries": max_seed_entries,
        "max_seed_bytes": max_seed_bytes,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "selected_items": [asdict(item) for item in selected],
        "candidates_hash": candidates_hash,
        "execution_authorized": False,
    }
    return EvidenceCacheColdStartSeedProposal(
        decision=decision,
        reasons=tuple(unsigned["reasons"]),
        integrity_hash=integrity.integrity_hash,
        source_identity_hash=integrity.source_identity_hash,
        source_watermark=integrity.source_watermark,
        candidate_count=len(validated),
        selected_count=len(selected),
        omitted_count=len(validated) - len(selected),
        selected_total_bytes=selected_bytes,
        max_seed_entries=max_seed_entries,
        max_seed_bytes=max_seed_bytes,
        max_evidence_age_seconds=max_evidence_age_seconds,
        selected_items=tuple(selected),
        candidates_hash=candidates_hash,
        proposal_hash=_hash(unsigned),
    )


def validate_cache_cold_start_seed_proposal(proposal: Any) -> None:
    if not isinstance(proposal, EvidenceCacheColdStartSeedProposal):
        raise EvidenceCacheColdStartSeedError("SEED_RESULT_TYPE_INVALID")
    if proposal.execution_authorized is not False:
        raise EvidenceCacheColdStartSeedError("SEED_SAFETY_BOUNDARY_INVALID")
    if proposal.decision == "PROPOSE" and (proposal.reasons or not proposal.selected_items):
        raise EvidenceCacheColdStartSeedError("PROPOSE_STATE_INVALID")
    if proposal.decision == "REJECT" and not proposal.reasons:
        raise EvidenceCacheColdStartSeedError("REJECT_REASONS_MISSING")
    if proposal.selected_count != len(proposal.selected_items):
        raise EvidenceCacheColdStartSeedError("SELECTED_COUNT_MISMATCH")
    unsigned = asdict(proposal)
    unsigned.pop("proposal_hash")
    unsigned["schema_version"] = SEED_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if proposal.proposal_hash != _hash(unsigned):
        raise EvidenceCacheColdStartSeedError("SEED_HASH_MISMATCH")


def _validated_candidate(value: Any) -> CacheSeedCandidate:
    if not isinstance(value, CacheSeedCandidate):
        raise EvidenceCacheColdStartSeedError("CANDIDATE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("candidate_hash")
    _validate_candidate_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise EvidenceCacheColdStartSeedError("CANDIDATE_HASH_MISMATCH")
    return value


def _validate_candidate_fields(payload: dict[str, Any]) -> None:
    for key in (
        "cache_key_hash",
        "content_hash",
        "source_identity_hash",
        "source_watermark",
    ):
        if not isinstance(payload[key], str) or not payload[key]:
            raise EvidenceCacheColdStartSeedError("CANDIDATE_FIELD_INVALID")
    for key in ("evidence_age_seconds", "size_bytes", "priority"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvidenceCacheColdStartSeedError("CANDIDATE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
