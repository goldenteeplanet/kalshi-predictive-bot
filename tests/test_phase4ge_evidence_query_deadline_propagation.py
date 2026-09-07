from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.phase4cd.evidence_query_cancellation_boundaries import (
    build_query_cancellation_boundaries,
)
from kalshi_predictor.phase4cd.evidence_query_deadline_propagation import (
    EvidenceQueryDeadlinePropagationError,
    propagate_query_deadline,
    validate_query_deadline_propagation,
)
from kalshi_predictor.phase4cd.settled_count_contention_audit import (
    audit_settled_count_contention,
    make_query_sample,
)
from kalshi_predictor.phase4cd.sqlite_busy_timeout_evidence import (
    build_busy_timeout_evidence,
    make_busy_timeout_observation,
)
from kalshi_predictor.phase4cd.sqlite_read_transaction_budget import (
    build_sqlite_read_transaction_budget,
)


def test_valid_deadline_propagates_to_bounded_stages() -> None:
    result = propagate_query_deadline(
        boundaries=_boundaries(),
        stages=["prepare", "read", "render"],
        boundary_age_seconds=1,
        elapsed_ms=20,
        propagation_overhead_ms=5,
    )
    validate_query_deadline_propagation(result)
    assert result.decision == "PROPAGATE"
    assert result.remaining_ms == 75
    assert [item.remaining_ms for item in result.stage_deadlines] == [75, 75, 75]
    assert result.execution_authorized is False


def test_empty_partial_and_stage_bound_inputs_fail_closed() -> None:
    with pytest.raises(EvidenceQueryDeadlinePropagationError, match="STAGES_EMPTY"):
        propagate_query_deadline(
            boundaries=_boundaries(), stages=[], boundary_age_seconds=1, elapsed_ms=1
        )
    with pytest.raises(EvidenceQueryDeadlinePropagationError, match="BOUNDARY_INPUT_INVALID"):
        propagate_query_deadline(
            boundaries=None, stages=["read"], boundary_age_seconds=1, elapsed_ms=1
        )
    with pytest.raises(EvidenceQueryDeadlinePropagationError, match="STAGE_BOUND_EXCEEDED"):
        propagate_query_deadline(
            boundaries=_boundaries(),
            stages=["a", "b"],
            boundary_age_seconds=1,
            elapsed_ms=1,
            max_stages=1,
        )


def test_exact_age_and_one_ms_remaining_boundaries_propagate() -> None:
    result = propagate_query_deadline(
        boundaries=_boundaries(),
        stages=["read"],
        boundary_age_seconds=300,
        max_boundary_age_seconds=300,
        elapsed_ms=99,
    )
    assert result.decision == "PROPAGATE"
    assert result.remaining_ms == 1


def test_stale_and_expired_boundaries_deny() -> None:
    stale = propagate_query_deadline(
        boundaries=_boundaries(),
        stages=["read"],
        boundary_age_seconds=301,
        max_boundary_age_seconds=300,
        elapsed_ms=1,
    )
    assert stale.decision == "DENY"
    assert stale.reasons == ("BOUNDARY_EVIDENCE_STALE",)
    expired = propagate_query_deadline(
        boundaries=_boundaries(),
        stages=["read"],
        boundary_age_seconds=1,
        elapsed_ms=100,
    )
    assert expired.decision == "DENY"
    assert expired.reasons == ("DEADLINE_EXPIRED",)


def test_malformed_and_duplicate_stages_fail_closed() -> None:
    with pytest.raises(EvidenceQueryDeadlinePropagationError, match="PROPAGATION_FIELD_INVALID"):
        propagate_query_deadline(
            boundaries=_boundaries(),
            stages=["read"],
            boundary_age_seconds=-1,
            elapsed_ms=1,
        )
    with pytest.raises(EvidenceQueryDeadlinePropagationError, match="STAGE_DUPLICATE"):
        propagate_query_deadline(
            boundaries=_boundaries(),
            stages=["read", "read"],
            boundary_age_seconds=1,
            elapsed_ms=1,
        )


def test_tampered_and_denied_boundaries_fail_or_deny_closed() -> None:
    boundaries = _boundaries()
    with pytest.raises(EvidenceQueryDeadlinePropagationError, match="BOUNDARY_INPUT_INVALID"):
        propagate_query_deadline(
            boundaries=replace(boundaries, boundary_hash="0" * 64),
            stages=["read"],
            boundary_age_seconds=1,
            elapsed_ms=1,
        )
    denied = replace(
        boundaries,
        decision="DENY",
        reasons=("test",),
    )
    # Re-hashing a fabricated upstream denial is intentionally unavailable; validation fails closed.
    with pytest.raises(EvidenceQueryDeadlinePropagationError, match="BOUNDARY_INPUT_INVALID"):
        propagate_query_deadline(
            boundaries=denied,
            stages=["read"],
            boundary_age_seconds=1,
            elapsed_ms=1,
        )


def test_result_tampering_and_retry_contract_fail_closed() -> None:
    result = propagate_query_deadline(
        boundaries=_boundaries(),
        stages=["read"],
        boundary_age_seconds=1,
        elapsed_ms=1,
    )
    with pytest.raises(EvidenceQueryDeadlinePropagationError, match="PROPAGATION_HASH_MISMATCH"):
        validate_query_deadline_propagation(replace(result, propagation_hash="0" * 64))
    with pytest.raises(
        EvidenceQueryDeadlinePropagationError, match="PROPAGATION_RETRY_CONTRACT_INVALID"
    ):
        validate_query_deadline_propagation(replace(result, retry_count=1))


def test_propagator_has_no_clock_query_or_mutation_surface() -> None:
    names = set(propagate_query_deadline.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "execute", "monotonic", "open", "replace", "time", "unlink"}
    )


def _boundaries():
    sample = make_query_sample(
        query_fingerprint="sha256:settled-count-v1",
        source_identity_hash="a" * 64,
        source_watermark="paper_pnl:204",
        age_seconds=1,
        duration_ms=20,
        busy_events=0,
        result_count=203,
    )
    audit = audit_settled_count_contention([sample])
    budget = build_sqlite_read_transaction_budget(
        audit=audit, requested_rows=1, requested_duration_ms=100
    )
    observation = make_busy_timeout_observation(
        source_identity_hash="a" * 64,
        source_watermark="paper_pnl:204",
        age_seconds=1,
        configured_timeout_ms=0,
    )
    evidence = build_busy_timeout_evidence(budget=budget, observations=[observation])
    return build_query_cancellation_boundaries(
        budget=budget,
        timeout_evidence=evidence,
        cancel_after_ms=100,
        progress_check_interval_ms=10,
    )
