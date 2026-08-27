from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.sqlite_read_transaction_budget import (
    SQLiteReadTransactionBudget,
    validate_sqlite_read_transaction_budget,
)

EVIDENCE_SCHEMA_VERSION = "phase4gc-sqlite-busy-timeout-evidence-v1"
EvidenceStatus = Literal["VERIFIED", "MISMATCH", "STALE"]


class SQLiteBusyTimeoutEvidenceError(ValueError):
    """Stable fail-closed busy-timeout evidence error."""


@dataclass(frozen=True)
class BusyTimeoutObservation:
    source_identity_hash: str
    source_watermark: str
    age_seconds: int
    configured_timeout_ms: int
    observation_hash: str


@dataclass(frozen=True)
class SQLiteBusyTimeoutEvidence:
    status: EvidenceStatus
    reasons: tuple[str, ...]
    budget_hash: str
    source_identity_hash: str
    source_watermark: str
    observation_count: int
    expected_timeout_ms: int
    observed_timeouts_ms: tuple[int, ...]
    observed_max_age_seconds: int
    max_age_seconds: int
    observations_hash: str
    evidence_hash: str
    execution_authorized: bool = False


def make_busy_timeout_observation(
    *,
    source_identity_hash: str,
    source_watermark: str,
    age_seconds: int,
    configured_timeout_ms: int,
) -> BusyTimeoutObservation:
    unsigned = {
        "source_identity_hash": source_identity_hash,
        "source_watermark": source_watermark,
        "age_seconds": age_seconds,
        "configured_timeout_ms": configured_timeout_ms,
    }
    _validate_observation_fields(unsigned)
    return BusyTimeoutObservation(**unsigned, observation_hash=_hash(unsigned))


def build_busy_timeout_evidence(
    *,
    budget: Any,
    observations: Sequence[Any],
    max_observations: int = 16,
    max_age_seconds: int = 300,
) -> SQLiteBusyTimeoutEvidence:
    if max_observations <= 0 or max_age_seconds < 0:
        raise SQLiteBusyTimeoutEvidenceError("EVIDENCE_BOUND_INVALID")
    if not observations:
        raise SQLiteBusyTimeoutEvidenceError("OBSERVATIONS_EMPTY")
    if len(observations) > max_observations:
        raise SQLiteBusyTimeoutEvidenceError("OBSERVATION_BOUND_EXCEEDED")
    try:
        validate_sqlite_read_transaction_budget(budget)
    except (TypeError, ValueError) as exc:
        raise SQLiteBusyTimeoutEvidenceError("BUDGET_INPUT_INVALID") from exc
    if not isinstance(budget, SQLiteReadTransactionBudget):
        raise SQLiteBusyTimeoutEvidenceError("BUDGET_INPUT_INVALID")
    if budget.decision != "GRANT":
        raise SQLiteBusyTimeoutEvidenceError("BUDGET_NOT_GRANTED")
    validated = [_validated_observation(item) for item in observations]
    if any(item.source_identity_hash != budget.source_identity_hash for item in validated):
        raise SQLiteBusyTimeoutEvidenceError("SOURCE_IDENTITY_MISMATCH")
    if any(item.source_watermark != budget.source_watermark for item in validated):
        raise SQLiteBusyTimeoutEvidenceError("SOURCE_WATERMARK_MISMATCH")

    observed_age = max(item.age_seconds for item in validated)
    timeouts = tuple(sorted({item.configured_timeout_ms for item in validated}))
    reasons: list[str] = []
    if observed_age > max_age_seconds:
        reasons.append("OBSERVATION_STALE")
        status: EvidenceStatus = "STALE"
    else:
        if timeouts != (budget.busy_timeout_ms,):
            reasons.append("BUSY_TIMEOUT_MISMATCH")
        status = "MISMATCH" if reasons else "VERIFIED"
    observations_hash = _hash([asdict(item) for item in validated])
    unsigned = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "status": status,
        "reasons": sorted(reasons),
        "budget_hash": budget.budget_hash,
        "source_identity_hash": budget.source_identity_hash,
        "source_watermark": budget.source_watermark,
        "observation_count": len(validated),
        "expected_timeout_ms": budget.busy_timeout_ms,
        "observed_timeouts_ms": list(timeouts),
        "observed_max_age_seconds": observed_age,
        "max_age_seconds": max_age_seconds,
        "observations_hash": observations_hash,
        "execution_authorized": False,
    }
    return SQLiteBusyTimeoutEvidence(
        status=status,
        reasons=tuple(unsigned["reasons"]),
        budget_hash=budget.budget_hash,
        source_identity_hash=budget.source_identity_hash,
        source_watermark=budget.source_watermark,
        observation_count=len(validated),
        expected_timeout_ms=budget.busy_timeout_ms,
        observed_timeouts_ms=timeouts,
        observed_max_age_seconds=observed_age,
        max_age_seconds=max_age_seconds,
        observations_hash=observations_hash,
        evidence_hash=_hash(unsigned),
    )


def validate_busy_timeout_evidence(evidence: Any) -> None:
    if not isinstance(evidence, SQLiteBusyTimeoutEvidence):
        raise SQLiteBusyTimeoutEvidenceError("EVIDENCE_RESULT_TYPE_INVALID")
    if evidence.execution_authorized is not False:
        raise SQLiteBusyTimeoutEvidenceError("EVIDENCE_SAFETY_BOUNDARY_INVALID")
    if evidence.status == "VERIFIED" and evidence.reasons:
        raise SQLiteBusyTimeoutEvidenceError("VERIFIED_REASONS_INVALID")
    if evidence.status != "VERIFIED" and not evidence.reasons:
        raise SQLiteBusyTimeoutEvidenceError("NONVERIFIED_REASONS_MISSING")
    unsigned = asdict(evidence)
    unsigned.pop("evidence_hash")
    unsigned["schema_version"] = EVIDENCE_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    unsigned["observed_timeouts_ms"] = list(unsigned["observed_timeouts_ms"])
    if evidence.evidence_hash != _hash(unsigned):
        raise SQLiteBusyTimeoutEvidenceError("EVIDENCE_HASH_MISMATCH")


def _validated_observation(value: Any) -> BusyTimeoutObservation:
    if not isinstance(value, BusyTimeoutObservation):
        raise SQLiteBusyTimeoutEvidenceError("OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("observation_hash")
    _validate_observation_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise SQLiteBusyTimeoutEvidenceError("OBSERVATION_HASH_MISMATCH")
    return value


def _validate_observation_fields(payload: dict[str, Any]) -> None:
    for key in ("source_identity_hash", "source_watermark"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise SQLiteBusyTimeoutEvidenceError("OBSERVATION_FIELD_INVALID")
    for key in ("age_seconds", "configured_timeout_ms"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SQLiteBusyTimeoutEvidenceError("OBSERVATION_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
