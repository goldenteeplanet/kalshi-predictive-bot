from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.phase4cd.evidence_query_cancellation_boundaries import (
    EvidenceQueryCancellationBoundaryError,
    build_query_cancellation_boundaries,
    validate_query_cancellation_boundaries,
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


def test_valid_boundaries_arm_with_no_retries() -> None:
    budget, evidence = _inputs()
    boundaries = build_query_cancellation_boundaries(
        budget=budget,
        timeout_evidence=evidence,
        cancel_after_ms=100,
        progress_check_interval_ms=10,
    )
    validate_query_cancellation_boundaries(boundaries)
    assert boundaries.decision == "ARM"
    assert boundaries.max_progress_callbacks == 10
    assert boundaries.retry_count == 0
    assert boundaries.execution_authorized is False


def test_empty_and_partial_inputs_fail_closed() -> None:
    budget, evidence = _inputs()
    for values in ((None, evidence), (budget, None), (None, None)):
        with pytest.raises(
            EvidenceQueryCancellationBoundaryError, match="BOUNDARY_INPUT_INVALID"
        ):
            build_query_cancellation_boundaries(
                budget=values[0],
                timeout_evidence=values[1],
                cancel_after_ms=100,
                progress_check_interval_ms=10,
            )


def test_exact_duration_and_callback_boundaries_arm() -> None:
    budget, evidence = _inputs()
    boundaries = build_query_cancellation_boundaries(
        budget=budget,
        timeout_evidence=evidence,
        cancel_after_ms=100,
        progress_check_interval_ms=1,
        max_callbacks=100,
    )
    assert boundaries.decision == "ARM"


@pytest.mark.parametrize(
    ("cancel", "interval", "callbacks", "reason"),
    [
        (101, 10, 128, "CANCELLATION_DEADLINE_EXCEEDS_BUDGET"),
        (100, 101, 128, "PROGRESS_INTERVAL_EXCEEDS_DEADLINE"),
        (100, 1, 99, "CALLBACK_BOUND_EXCEEDED"),
    ],
)
def test_boundary_breaches_deny(
    cancel: int, interval: int, callbacks: int, reason: str
) -> None:
    budget, evidence = _inputs()
    boundaries = build_query_cancellation_boundaries(
        budget=budget,
        timeout_evidence=evidence,
        cancel_after_ms=cancel,
        progress_check_interval_ms=interval,
        max_callbacks=callbacks,
    )
    assert boundaries.decision == "DENY"
    assert reason in boundaries.reasons


def test_stale_evidence_denies_and_malformed_fields_fail_closed() -> None:
    budget, stale = _inputs(observation_age=301, evidence_max_age=300)
    boundaries = build_query_cancellation_boundaries(
        budget=budget,
        timeout_evidence=stale,
        cancel_after_ms=100,
        progress_check_interval_ms=10,
    )
    assert boundaries.decision == "DENY"
    assert boundaries.reasons == ("TIMEOUT_EVIDENCE_STALE",)
    with pytest.raises(EvidenceQueryCancellationBoundaryError, match="BOUNDARY_FIELD_INVALID"):
        build_query_cancellation_boundaries(
            budget=budget,
            timeout_evidence=stale,
            cancel_after_ms=0,
            progress_check_interval_ms=10,
        )


def test_tampering_and_cross_link_failure_fail_closed() -> None:
    budget, evidence = _inputs()
    with pytest.raises(
        EvidenceQueryCancellationBoundaryError, match="BOUNDARY_INPUT_INVALID"
    ):
        build_query_cancellation_boundaries(
            budget=budget,
            timeout_evidence=replace(evidence, evidence_hash="0" * 64),
            cancel_after_ms=100,
            progress_check_interval_ms=10,
        )
    other_budget, _ = _inputs(watermark="other")
    with pytest.raises(
        EvidenceQueryCancellationBoundaryError, match="BUDGET_EVIDENCE_LINK_MISMATCH"
    ):
        build_query_cancellation_boundaries(
            budget=other_budget,
            timeout_evidence=evidence,
            cancel_after_ms=100,
            progress_check_interval_ms=10,
        )


def test_result_tampering_and_safety_contract_fail_closed() -> None:
    budget, evidence = _inputs()
    boundaries = build_query_cancellation_boundaries(
        budget=budget,
        timeout_evidence=evidence,
        cancel_after_ms=100,
        progress_check_interval_ms=10,
    )
    with pytest.raises(EvidenceQueryCancellationBoundaryError, match="BOUNDARY_HASH_MISMATCH"):
        validate_query_cancellation_boundaries(
            replace(boundaries, boundary_hash="0" * 64)
        )
    with pytest.raises(
        EvidenceQueryCancellationBoundaryError, match="BOUNDARY_RETRY_CONTRACT_INVALID"
    ):
        validate_query_cancellation_boundaries(replace(boundaries, retry_count=1))


def test_boundary_builder_has_no_query_or_mutation_surface() -> None:
    names = set(build_query_cancellation_boundaries.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "execute", "open", "replace", "sqlite3", "unlink"}
    )


def _inputs(
    *,
    observation_age: int = 1,
    evidence_max_age: int = 300,
    watermark: str = "paper_pnl:204",
):
    sample = make_query_sample(
        query_fingerprint="sha256:settled-count-v1",
        source_identity_hash="a" * 64,
        source_watermark=watermark,
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
        source_watermark=watermark,
        age_seconds=observation_age,
        configured_timeout_ms=0,
    )
    evidence = build_busy_timeout_evidence(
        budget=budget,
        observations=[observation],
        max_age_seconds=evidence_max_age,
    )
    return budget, evidence
