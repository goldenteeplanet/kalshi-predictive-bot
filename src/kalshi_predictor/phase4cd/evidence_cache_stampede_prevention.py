from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.evidence_query_deadline_propagation import (
    EvidenceQueryDeadlinePropagation,
    validate_query_deadline_propagation,
)

POLICY_SCHEMA_VERSION = "phase4gf-evidence-cache-stampede-prevention-v1"
CacheDecision = Literal["CACHE_HIT", "ELECT_ONE", "WAIT"]


class EvidenceCacheStampedeError(ValueError):
    """Stable fail-closed cache-stampede policy error."""


@dataclass(frozen=True)
class EvidenceCacheSnapshot:
    cache_key_hash: str
    source_identity_hash: str
    source_watermark: str
    cache_age_seconds: int
    refresh_inflight: bool
    refresh_lease_age_seconds: int | None
    snapshot_hash: str


@dataclass(frozen=True)
class EvidenceCacheStampedePolicy:
    decision: CacheDecision
    reasons: tuple[str, ...]
    propagation_hash: str
    snapshot_hash: str
    cache_key_hash: str
    source_identity_hash: str
    source_watermark: str
    cache_age_seconds: int
    max_cache_age_seconds: int
    refresh_lease_ttl_seconds: int
    contender_count: int
    elected_requester: str | None
    policy_hash: str
    execution_authorized: bool = False


def make_cache_snapshot(
    *,
    cache_key_hash: str,
    source_identity_hash: str,
    source_watermark: str,
    cache_age_seconds: int,
    refresh_inflight: bool,
    refresh_lease_age_seconds: int | None,
) -> EvidenceCacheSnapshot:
    unsigned = {
        "cache_key_hash": cache_key_hash,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "cache_age_seconds": cache_age_seconds,
        "refresh_inflight": refresh_inflight,
        "refresh_lease_age_seconds": refresh_lease_age_seconds,
    }
    _validate_snapshot_fields(unsigned)
    return EvidenceCacheSnapshot(**unsigned, snapshot_hash=_hash(unsigned))


def prevent_evidence_cache_stampede(
    *,
    propagation: Any,
    snapshot: Any,
    requester_ids: Sequence[Any],
    max_requesters: int = 32,
    max_cache_age_seconds: int = 60,
    refresh_lease_ttl_seconds: int = 30,
) -> EvidenceCacheStampedePolicy:
    if max_requesters <= 0 or max_cache_age_seconds < 0 or refresh_lease_ttl_seconds < 0:
        raise EvidenceCacheStampedeError("POLICY_BOUND_INVALID")
    if not requester_ids:
        raise EvidenceCacheStampedeError("REQUESTERS_EMPTY")
    if len(requester_ids) > max_requesters:
        raise EvidenceCacheStampedeError("REQUESTER_BOUND_EXCEEDED")
    if any(not isinstance(item, str) or not item.strip() for item in requester_ids):
        raise EvidenceCacheStampedeError("REQUESTER_INVALID")
    requesters = tuple(item.strip() for item in requester_ids)
    if len(set(requesters)) != len(requesters):
        raise EvidenceCacheStampedeError("REQUESTER_DUPLICATE")
    try:
        validate_query_deadline_propagation(propagation)
    except (TypeError, ValueError) as exc:
        raise EvidenceCacheStampedeError("PROPAGATION_INPUT_INVALID") from exc
    if not isinstance(propagation, EvidenceQueryDeadlinePropagation):
        raise EvidenceCacheStampedeError("PROPAGATION_INPUT_INVALID")
    if propagation.decision != "PROPAGATE":
        raise EvidenceCacheStampedeError("PROPAGATION_NOT_ACTIVE")
    validated_snapshot = _validated_snapshot(snapshot)
    if validated_snapshot.source_identity_hash != propagation.source_identity_hash:
        raise EvidenceCacheStampedeError("SOURCE_IDENTITY_MISMATCH")
    if validated_snapshot.source_watermark != propagation.source_watermark:
        raise EvidenceCacheStampedeError("SOURCE_WATERMARK_MISMATCH")

    stale = validated_snapshot.cache_age_seconds > max_cache_age_seconds
    lease_live = (
        validated_snapshot.refresh_inflight
        and validated_snapshot.refresh_lease_age_seconds is not None
        and validated_snapshot.refresh_lease_age_seconds <= refresh_lease_ttl_seconds
    )
    if not stale:
        decision: CacheDecision = "CACHE_HIT"
        elected = None
        reasons = ["CACHE_FRESH"]
    elif lease_live:
        decision = "WAIT"
        elected = None
        reasons = ["REFRESH_LEASE_LIVE"]
    else:
        decision = "ELECT_ONE"
        elected = min(requesters, key=lambda value: (_hash(value), value))
        reasons = ["CACHE_STALE", "SINGLE_REFRESH_ELECTION"]
    unsigned = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "decision": decision,
        "reasons": sorted(reasons),
        "propagation_hash": propagation.propagation_hash,
        "snapshot_hash": validated_snapshot.snapshot_hash,
        "cache_key_hash": validated_snapshot.cache_key_hash,
        "source_identity_hash": propagation.source_identity_hash,
        "source_watermark": propagation.source_watermark,
        "cache_age_seconds": validated_snapshot.cache_age_seconds,
        "max_cache_age_seconds": max_cache_age_seconds,
        "refresh_lease_ttl_seconds": refresh_lease_ttl_seconds,
        "contender_count": len(requesters),
        "elected_requester": elected,
        "execution_authorized": False,
    }
    return EvidenceCacheStampedePolicy(
        decision=decision,
        reasons=tuple(unsigned["reasons"]),
        propagation_hash=propagation.propagation_hash,
        snapshot_hash=validated_snapshot.snapshot_hash,
        cache_key_hash=validated_snapshot.cache_key_hash,
        source_identity_hash=propagation.source_identity_hash,
        source_watermark=propagation.source_watermark,
        cache_age_seconds=validated_snapshot.cache_age_seconds,
        max_cache_age_seconds=max_cache_age_seconds,
        refresh_lease_ttl_seconds=refresh_lease_ttl_seconds,
        contender_count=len(requesters),
        elected_requester=elected,
        policy_hash=_hash(unsigned),
    )


def validate_stampede_policy(policy: Any) -> None:
    if not isinstance(policy, EvidenceCacheStampedePolicy):
        raise EvidenceCacheStampedeError("POLICY_RESULT_TYPE_INVALID")
    if policy.execution_authorized is not False:
        raise EvidenceCacheStampedeError("POLICY_SAFETY_BOUNDARY_INVALID")
    if policy.decision == "ELECT_ONE" and policy.elected_requester is None:
        raise EvidenceCacheStampedeError("ELECTION_WINNER_MISSING")
    if policy.decision != "ELECT_ONE" and policy.elected_requester is not None:
        raise EvidenceCacheStampedeError("UNEXPECTED_ELECTION_WINNER")
    unsigned = asdict(policy)
    unsigned.pop("policy_hash")
    unsigned["schema_version"] = POLICY_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if policy.policy_hash != _hash(unsigned):
        raise EvidenceCacheStampedeError("POLICY_HASH_MISMATCH")


def _validated_snapshot(value: Any) -> EvidenceCacheSnapshot:
    if not isinstance(value, EvidenceCacheSnapshot):
        raise EvidenceCacheStampedeError("SNAPSHOT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("snapshot_hash")
    _validate_snapshot_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise EvidenceCacheStampedeError("SNAPSHOT_HASH_MISMATCH")
    return value


def _validate_snapshot_fields(payload: dict[str, Any]) -> None:
    for key in ("cache_key_hash", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise EvidenceCacheStampedeError("SNAPSHOT_FIELD_INVALID")
    if isinstance(payload["cache_age_seconds"], bool) or not isinstance(
        payload["cache_age_seconds"], int
    ) or payload["cache_age_seconds"] < 0:
        raise EvidenceCacheStampedeError("SNAPSHOT_FIELD_INVALID")
    if not isinstance(payload["refresh_inflight"], bool):
        raise EvidenceCacheStampedeError("SNAPSHOT_FIELD_INVALID")
    lease_age = payload["refresh_lease_age_seconds"]
    if lease_age is not None and (
        isinstance(lease_age, bool) or not isinstance(lease_age, int) or lease_age < 0
    ):
        raise EvidenceCacheStampedeError("SNAPSHOT_FIELD_INVALID")
    if payload["refresh_inflight"] and lease_age is None:
        raise EvidenceCacheStampedeError("LEASE_AGE_MISSING")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
