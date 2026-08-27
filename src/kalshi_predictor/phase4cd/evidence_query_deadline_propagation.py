from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.evidence_query_cancellation_boundaries import (
    EvidenceQueryCancellationBoundaries,
    validate_query_cancellation_boundaries,
)

PROPAGATION_SCHEMA_VERSION = "phase4ge-evidence-query-deadline-propagation-v1"
PropagationDecision = Literal["PROPAGATE", "DENY"]


class EvidenceQueryDeadlinePropagationError(ValueError):
    """Stable fail-closed deadline-propagation error."""


@dataclass(frozen=True)
class StageDeadline:
    stage: str
    remaining_ms: int


@dataclass(frozen=True)
class EvidenceQueryDeadlinePropagation:
    decision: PropagationDecision
    reasons: tuple[str, ...]
    boundary_hash: str
    source_identity_hash: str
    source_watermark: str
    boundary_age_seconds: int
    max_boundary_age_seconds: int
    elapsed_ms: int
    propagation_overhead_ms: int
    remaining_ms: int
    stage_deadlines: tuple[StageDeadline, ...]
    retry_count: int
    propagation_hash: str
    execution_authorized: bool = False


def propagate_query_deadline(
    *,
    boundaries: Any,
    stages: Sequence[Any],
    boundary_age_seconds: int,
    elapsed_ms: int,
    propagation_overhead_ms: int = 0,
    max_boundary_age_seconds: int = 300,
    max_stages: int = 8,
) -> EvidenceQueryDeadlinePropagation:
    for value in (
        boundary_age_seconds,
        elapsed_ms,
        propagation_overhead_ms,
        max_boundary_age_seconds,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvidenceQueryDeadlinePropagationError("PROPAGATION_FIELD_INVALID")
    if max_stages <= 0:
        raise EvidenceQueryDeadlinePropagationError("STAGE_BOUND_INVALID")
    if not stages:
        raise EvidenceQueryDeadlinePropagationError("STAGES_EMPTY")
    if len(stages) > max_stages:
        raise EvidenceQueryDeadlinePropagationError("STAGE_BOUND_EXCEEDED")
    if any(not isinstance(stage, str) or not stage.strip() for stage in stages):
        raise EvidenceQueryDeadlinePropagationError("STAGE_INVALID")
    normalized = tuple(stage.strip() for stage in stages)
    if len(set(normalized)) != len(normalized):
        raise EvidenceQueryDeadlinePropagationError("STAGE_DUPLICATE")
    try:
        validate_query_cancellation_boundaries(boundaries)
    except (TypeError, ValueError) as exc:
        raise EvidenceQueryDeadlinePropagationError("BOUNDARY_INPUT_INVALID") from exc
    if not isinstance(boundaries, EvidenceQueryCancellationBoundaries):
        raise EvidenceQueryDeadlinePropagationError("BOUNDARY_INPUT_INVALID")

    remaining = max(
        0, boundaries.cancel_after_ms - elapsed_ms - propagation_overhead_ms
    )
    reasons: list[str] = []
    if boundaries.decision != "ARM":
        reasons.append("BOUNDARY_NOT_ARMED")
    if boundary_age_seconds > max_boundary_age_seconds:
        reasons.append("BOUNDARY_EVIDENCE_STALE")
    if remaining == 0:
        reasons.append("DEADLINE_EXPIRED")
    decision: PropagationDecision = "PROPAGATE" if not reasons else "DENY"
    stage_deadlines = tuple(StageDeadline(stage, remaining) for stage in normalized)
    unsigned = {
        "schema_version": PROPAGATION_SCHEMA_VERSION,
        "decision": decision,
        "reasons": sorted(reasons),
        "boundary_hash": boundaries.boundary_hash,
        "source_identity_hash": boundaries.source_identity_hash,
        "source_watermark": boundaries.source_watermark,
        "boundary_age_seconds": boundary_age_seconds,
        "max_boundary_age_seconds": max_boundary_age_seconds,
        "elapsed_ms": elapsed_ms,
        "propagation_overhead_ms": propagation_overhead_ms,
        "remaining_ms": remaining,
        "stage_deadlines": [asdict(item) for item in stage_deadlines],
        "retry_count": 0,
        "execution_authorized": False,
    }
    return EvidenceQueryDeadlinePropagation(
        decision=decision,
        reasons=tuple(unsigned["reasons"]),
        boundary_hash=boundaries.boundary_hash,
        source_identity_hash=boundaries.source_identity_hash,
        source_watermark=boundaries.source_watermark,
        boundary_age_seconds=boundary_age_seconds,
        max_boundary_age_seconds=max_boundary_age_seconds,
        elapsed_ms=elapsed_ms,
        propagation_overhead_ms=propagation_overhead_ms,
        remaining_ms=remaining,
        stage_deadlines=stage_deadlines,
        retry_count=0,
        propagation_hash=_hash(unsigned),
    )


def validate_query_deadline_propagation(propagation: Any) -> None:
    if not isinstance(propagation, EvidenceQueryDeadlinePropagation):
        raise EvidenceQueryDeadlinePropagationError("PROPAGATION_RESULT_TYPE_INVALID")
    if propagation.execution_authorized is not False:
        raise EvidenceQueryDeadlinePropagationError("PROPAGATION_SAFETY_CONTRACT_INVALID")
    if propagation.retry_count != 0:
        raise EvidenceQueryDeadlinePropagationError("PROPAGATION_RETRY_CONTRACT_INVALID")
    if propagation.decision == "PROPAGATE" and propagation.reasons:
        raise EvidenceQueryDeadlinePropagationError("PROPAGATE_REASONS_INVALID")
    if propagation.decision == "DENY" and not propagation.reasons:
        raise EvidenceQueryDeadlinePropagationError("DENY_REASONS_MISSING")
    unsigned = asdict(propagation)
    unsigned.pop("propagation_hash")
    unsigned["schema_version"] = PROPAGATION_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if propagation.propagation_hash != _hash(unsigned):
        raise EvidenceQueryDeadlinePropagationError("PROPAGATION_HASH_MISMATCH")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
