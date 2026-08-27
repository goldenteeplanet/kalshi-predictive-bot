from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

AUDIT_SCHEMA_VERSION = "phase4ga-settled-count-query-contention-audit-v1"
AuditStatus = Literal["CLEAR", "CONTENDED", "STALE"]


class SettledCountContentionAuditError(ValueError):
    """Stable fail-closed contention-audit error."""


@dataclass(frozen=True)
class SettledCountQuerySample:
    query_fingerprint: str
    source_identity_hash: str
    source_watermark: str
    age_seconds: int
    duration_ms: int
    busy_events: int
    result_count: int
    sample_hash: str


@dataclass(frozen=True)
class SettledCountContentionAudit:
    status: AuditStatus
    reasons: tuple[str, ...]
    sample_count: int
    query_fingerprint: str
    source_identity_hash: str
    source_watermark: str
    max_age_seconds: int
    max_duration_ms: int
    observed_max_age_seconds: int
    observed_max_duration_ms: int
    total_busy_events: int
    samples_hash: str
    audit_hash: str
    execution_authorized: bool = False


def make_query_sample(
    *,
    query_fingerprint: str,
    source_identity_hash: str,
    source_watermark: str,
    age_seconds: int,
    duration_ms: int,
    busy_events: int,
    result_count: int,
) -> SettledCountQuerySample:
    unsigned = {
        "query_fingerprint": query_fingerprint,
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "age_seconds": age_seconds,
        "duration_ms": duration_ms,
        "busy_events": busy_events,
        "result_count": result_count,
    }
    _validate_sample_fields(unsigned)
    return SettledCountQuerySample(**unsigned, sample_hash=_hash(unsigned))


def audit_settled_count_contention(
    samples: Sequence[Any],
    *,
    max_samples: int = 64,
    max_age_seconds: int = 300,
    max_duration_ms: int = 250,
) -> SettledCountContentionAudit:
    if max_samples <= 0 or max_age_seconds < 0 or max_duration_ms < 0:
        raise SettledCountContentionAuditError("AUDIT_BOUND_INVALID")
    if not samples:
        raise SettledCountContentionAuditError("AUDIT_INPUT_EMPTY")
    if len(samples) > max_samples:
        raise SettledCountContentionAuditError("SAMPLE_BOUND_EXCEEDED")
    validated = [_validated_sample(sample) for sample in samples]
    fingerprints = {sample.query_fingerprint for sample in validated}
    identities = {sample.source_identity_hash for sample in validated}
    watermarks = {sample.source_watermark for sample in validated}
    if len(fingerprints) != 1:
        raise SettledCountContentionAuditError("QUERY_LINEAGE_MIXED")
    if len(identities) != 1 or len(watermarks) != 1:
        raise SettledCountContentionAuditError("SOURCE_LINEAGE_MIXED")

    observed_age = max(sample.age_seconds for sample in validated)
    observed_duration = max(sample.duration_ms for sample in validated)
    busy_events = sum(sample.busy_events for sample in validated)
    reasons: list[str] = []
    if observed_age > max_age_seconds:
        reasons.append("EVIDENCE_STALE")
        status: AuditStatus = "STALE"
    else:
        if observed_duration > max_duration_ms:
            reasons.append("LATENCY_THRESHOLD_EXCEEDED")
        if busy_events:
            reasons.append("BUSY_EVENTS_OBSERVED")
        status = "CONTENDED" if reasons else "CLEAR"
    samples_hash = _hash([asdict(sample) for sample in validated])
    unsigned = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "sample_count": len(validated),
        "query_fingerprint": validated[0].query_fingerprint,
        "source_identity_hash": validated[0].source_identity_hash,
        "source_watermark": validated[0].source_watermark,
        "max_age_seconds": max_age_seconds,
        "max_duration_ms": max_duration_ms,
        "observed_max_age_seconds": observed_age,
        "observed_max_duration_ms": observed_duration,
        "total_busy_events": busy_events,
        "samples_hash": samples_hash,
        "execution_authorized": False,
    }
    return SettledCountContentionAudit(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        sample_count=len(validated),
        query_fingerprint=validated[0].query_fingerprint,
        source_identity_hash=validated[0].source_identity_hash,
        source_watermark=validated[0].source_watermark,
        max_age_seconds=max_age_seconds,
        max_duration_ms=max_duration_ms,
        observed_max_age_seconds=observed_age,
        observed_max_duration_ms=observed_duration,
        total_busy_events=busy_events,
        samples_hash=samples_hash,
        audit_hash=_hash(unsigned),
    )


def validate_contention_audit(audit: Any) -> None:
    if not isinstance(audit, SettledCountContentionAudit):
        raise SettledCountContentionAuditError("AUDIT_RESULT_TYPE_INVALID")
    if audit.execution_authorized is not False:
        raise SettledCountContentionAuditError("AUDIT_SAFETY_BOUNDARY_INVALID")
    if audit.status == "CLEAR" and audit.reasons:
        raise SettledCountContentionAuditError("CLEAR_REASONS_INVALID")
    if audit.status != "CLEAR" and not audit.reasons:
        raise SettledCountContentionAuditError("NONCLEAR_REASONS_MISSING")
    unsigned = asdict(audit)
    unsigned.pop("audit_hash")
    unsigned["schema_version"] = AUDIT_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if audit.audit_hash != _hash(unsigned):
        raise SettledCountContentionAuditError("AUDIT_HASH_MISMATCH")


def _validated_sample(value: Any) -> SettledCountQuerySample:
    if not isinstance(value, SettledCountQuerySample):
        raise SettledCountContentionAuditError("SAMPLE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("sample_hash")
    _validate_sample_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise SettledCountContentionAuditError("SAMPLE_HASH_MISMATCH")
    return value


def _validate_sample_fields(payload: dict[str, Any]) -> None:
    for key in ("query_fingerprint", "source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise SettledCountContentionAuditError("SAMPLE_FIELD_INVALID")
    for key in ("age_seconds", "duration_ms", "busy_events", "result_count"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SettledCountContentionAuditError("SAMPLE_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
