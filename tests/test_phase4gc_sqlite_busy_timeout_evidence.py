from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.phase4cd.settled_count_contention_audit import (
    audit_settled_count_contention,
    make_query_sample,
)
from kalshi_predictor.phase4cd.sqlite_busy_timeout_evidence import (
    SQLiteBusyTimeoutEvidenceError,
    build_busy_timeout_evidence,
    make_busy_timeout_observation,
    validate_busy_timeout_evidence,
)
from kalshi_predictor.phase4cd.sqlite_read_transaction_budget import (
    build_sqlite_read_transaction_budget,
)


def test_zero_timeout_evidence_is_verified() -> None:
    evidence = build_busy_timeout_evidence(
        budget=_budget(), observations=[_observation(), _observation(age=2)]
    )
    validate_busy_timeout_evidence(evidence)
    assert evidence.status == "VERIFIED"
    assert evidence.observed_timeouts_ms == (0,)
    assert evidence.execution_authorized is False


def test_empty_partial_and_bounded_inputs_fail_closed() -> None:
    with pytest.raises(SQLiteBusyTimeoutEvidenceError, match="OBSERVATIONS_EMPTY"):
        build_busy_timeout_evidence(budget=_budget(), observations=[])
    with pytest.raises(SQLiteBusyTimeoutEvidenceError, match="BUDGET_INPUT_INVALID"):
        build_busy_timeout_evidence(budget=None, observations=[_observation()])
    with pytest.raises(SQLiteBusyTimeoutEvidenceError, match="OBSERVATION_BOUND_EXCEEDED"):
        build_busy_timeout_evidence(
            budget=_budget(), observations=[_observation(), _observation()], max_observations=1
        )


def test_exact_freshness_boundary_is_verified() -> None:
    evidence = build_busy_timeout_evidence(
        budget=_budget(), observations=[_observation(age=300)], max_age_seconds=300
    )
    assert evidence.status == "VERIFIED"


def test_stale_and_timeout_mismatch_are_explicit() -> None:
    stale = build_busy_timeout_evidence(
        budget=_budget(), observations=[_observation(age=301)], max_age_seconds=300
    )
    assert stale.status == "STALE"
    assert stale.reasons == ("OBSERVATION_STALE",)
    mismatch = build_busy_timeout_evidence(budget=_budget(), observations=[_observation(timeout=1)])
    assert mismatch.status == "MISMATCH"
    assert mismatch.reasons == ("BUSY_TIMEOUT_MISMATCH",)


def test_malformed_observation_and_invalid_bound_fail_closed() -> None:
    with pytest.raises(SQLiteBusyTimeoutEvidenceError, match="OBSERVATION_FIELD_INVALID"):
        make_busy_timeout_observation(
            source_identity_hash="a" * 64,
            source_watermark="paper_pnl:204",
            age_seconds=-1,
            configured_timeout_ms=0,
        )
    with pytest.raises(SQLiteBusyTimeoutEvidenceError, match="EVIDENCE_BOUND_INVALID"):
        build_busy_timeout_evidence(
            budget=_budget(), observations=[_observation()], max_age_seconds=-1
        )


def test_tampering_and_lineage_mismatch_fail_closed() -> None:
    observation = _observation()
    with pytest.raises(SQLiteBusyTimeoutEvidenceError, match="OBSERVATION_HASH_MISMATCH"):
        build_busy_timeout_evidence(
            budget=_budget(),
            observations=[replace(observation, configured_timeout_ms=1)],
        )
    with pytest.raises(SQLiteBusyTimeoutEvidenceError, match="SOURCE_WATERMARK_MISMATCH"):
        build_busy_timeout_evidence(
            budget=_budget(), observations=[_observation(watermark="other")]
        )


def test_denied_budget_fails_closed() -> None:
    denied = build_sqlite_read_transaction_budget(
        audit=_audit(), requested_rows=2, requested_duration_ms=100
    )
    with pytest.raises(SQLiteBusyTimeoutEvidenceError, match="BUDGET_NOT_GRANTED"):
        build_busy_timeout_evidence(budget=denied, observations=[_observation()])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    evidence = build_busy_timeout_evidence(budget=_budget(), observations=[_observation()])
    with pytest.raises(SQLiteBusyTimeoutEvidenceError, match="EVIDENCE_HASH_MISMATCH"):
        validate_busy_timeout_evidence(replace(evidence, evidence_hash="0" * 64))
    with pytest.raises(SQLiteBusyTimeoutEvidenceError, match="EVIDENCE_SAFETY_BOUNDARY_INVALID"):
        validate_busy_timeout_evidence(replace(evidence, execution_authorized=True))


def test_evidence_builder_has_no_sqlite_or_mutation_surface() -> None:
    names = set(build_busy_timeout_evidence.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "execute", "open", "replace", "sqlite3", "unlink"}
    )


def _audit():
    sample = make_query_sample(
        query_fingerprint="sha256:settled-count-v1",
        source_identity_hash="a" * 64,
        source_watermark="paper_pnl:204",
        age_seconds=1,
        duration_ms=20,
        busy_events=0,
        result_count=203,
    )
    return audit_settled_count_contention([sample])


def _budget():
    return build_sqlite_read_transaction_budget(
        audit=_audit(), requested_rows=1, requested_duration_ms=100
    )


def _observation(*, age: int = 1, timeout: int = 0, watermark: str = "paper_pnl:204"):
    return make_busy_timeout_observation(
        source_identity_hash="a" * 64,
        source_watermark=watermark,
        age_seconds=age,
        configured_timeout_ms=timeout,
    )
