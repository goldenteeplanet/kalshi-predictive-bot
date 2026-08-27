from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.phase4cd.sqlite_busy_timeout_evidence import (
    SQLiteBusyTimeoutEvidence,
    validate_busy_timeout_evidence,
)
from kalshi_predictor.phase4cd.sqlite_read_transaction_budget import (
    SQLiteReadTransactionBudget,
    validate_sqlite_read_transaction_budget,
)

BOUNDARY_SCHEMA_VERSION = "phase4gd-evidence-query-cancellation-boundaries-v1"
BoundaryDecision = Literal["ARM", "DENY"]


class EvidenceQueryCancellationBoundaryError(ValueError):
    """Stable fail-closed query-cancellation boundary error."""


@dataclass(frozen=True)
class EvidenceQueryCancellationBoundaries:
    decision: BoundaryDecision
    reasons: tuple[str, ...]
    budget_hash: str
    timeout_evidence_hash: str
    source_identity_hash: str
    source_watermark: str
    cancel_after_ms: int
    progress_check_interval_ms: int
    max_progress_callbacks: int
    retry_count: int
    boundary_hash: str
    execution_authorized: bool = False


def build_query_cancellation_boundaries(
    *,
    budget: Any,
    timeout_evidence: Any,
    cancel_after_ms: int,
    progress_check_interval_ms: int,
    max_callbacks: int = 128,
) -> EvidenceQueryCancellationBoundaries:
    for value in (cancel_after_ms, progress_check_interval_ms, max_callbacks):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EvidenceQueryCancellationBoundaryError("BOUNDARY_FIELD_INVALID")
    try:
        validate_sqlite_read_transaction_budget(budget)
        validate_busy_timeout_evidence(timeout_evidence)
    except (TypeError, ValueError) as exc:
        raise EvidenceQueryCancellationBoundaryError("BOUNDARY_INPUT_INVALID") from exc
    if not isinstance(budget, SQLiteReadTransactionBudget):
        raise EvidenceQueryCancellationBoundaryError("BOUNDARY_INPUT_INVALID")
    if not isinstance(timeout_evidence, SQLiteBusyTimeoutEvidence):
        raise EvidenceQueryCancellationBoundaryError("BOUNDARY_INPUT_INVALID")
    if timeout_evidence.budget_hash != budget.budget_hash:
        raise EvidenceQueryCancellationBoundaryError("BUDGET_EVIDENCE_LINK_MISMATCH")
    if timeout_evidence.source_identity_hash != budget.source_identity_hash:
        raise EvidenceQueryCancellationBoundaryError("SOURCE_IDENTITY_LINK_MISMATCH")
    if timeout_evidence.source_watermark != budget.source_watermark:
        raise EvidenceQueryCancellationBoundaryError("SOURCE_WATERMARK_LINK_MISMATCH")

    callbacks = (cancel_after_ms + progress_check_interval_ms - 1) // progress_check_interval_ms
    reasons: list[str] = []
    if budget.decision != "GRANT":
        reasons.append("BUDGET_NOT_GRANTED")
    if timeout_evidence.status != "VERIFIED":
        reasons.append(f"TIMEOUT_EVIDENCE_{timeout_evidence.status}")
    if cancel_after_ms > budget.max_duration_ms:
        reasons.append("CANCELLATION_DEADLINE_EXCEEDS_BUDGET")
    if progress_check_interval_ms > cancel_after_ms:
        reasons.append("PROGRESS_INTERVAL_EXCEEDS_DEADLINE")
    if callbacks > max_callbacks:
        reasons.append("CALLBACK_BOUND_EXCEEDED")
    decision: BoundaryDecision = "ARM" if not reasons else "DENY"
    unsigned = {
        "schema_version": BOUNDARY_SCHEMA_VERSION,
        "decision": decision,
        "reasons": sorted(reasons),
        "budget_hash": budget.budget_hash,
        "timeout_evidence_hash": timeout_evidence.evidence_hash,
        "source_identity_hash": budget.source_identity_hash,
        "source_watermark": budget.source_watermark,
        "cancel_after_ms": cancel_after_ms,
        "progress_check_interval_ms": progress_check_interval_ms,
        "max_progress_callbacks": callbacks,
        "retry_count": 0,
        "execution_authorized": False,
    }
    return EvidenceQueryCancellationBoundaries(
        decision=decision,
        reasons=tuple(unsigned["reasons"]),
        budget_hash=budget.budget_hash,
        timeout_evidence_hash=timeout_evidence.evidence_hash,
        source_identity_hash=budget.source_identity_hash,
        source_watermark=budget.source_watermark,
        cancel_after_ms=cancel_after_ms,
        progress_check_interval_ms=progress_check_interval_ms,
        max_progress_callbacks=callbacks,
        retry_count=0,
        boundary_hash=_hash(unsigned),
    )


def validate_query_cancellation_boundaries(boundaries: Any) -> None:
    if not isinstance(boundaries, EvidenceQueryCancellationBoundaries):
        raise EvidenceQueryCancellationBoundaryError("BOUNDARY_RESULT_TYPE_INVALID")
    if boundaries.execution_authorized is not False:
        raise EvidenceQueryCancellationBoundaryError("BOUNDARY_SAFETY_CONTRACT_INVALID")
    if boundaries.retry_count != 0:
        raise EvidenceQueryCancellationBoundaryError("BOUNDARY_RETRY_CONTRACT_INVALID")
    if boundaries.decision == "ARM" and boundaries.reasons:
        raise EvidenceQueryCancellationBoundaryError("ARM_REASONS_INVALID")
    if boundaries.decision == "DENY" and not boundaries.reasons:
        raise EvidenceQueryCancellationBoundaryError("DENY_REASONS_MISSING")
    unsigned = asdict(boundaries)
    unsigned.pop("boundary_hash")
    unsigned["schema_version"] = BOUNDARY_SCHEMA_VERSION
    unsigned["reasons"] = sorted(unsigned["reasons"])
    if boundaries.boundary_hash != _hash(unsigned):
        raise EvidenceQueryCancellationBoundaryError("BOUNDARY_HASH_MISMATCH")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
