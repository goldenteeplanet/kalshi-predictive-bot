from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.phase4cd.settled_count_contention_audit import (
    SettledCountContentionAuditError,
    audit_settled_count_contention,
    make_query_sample,
    validate_contention_audit,
)


def test_clear_valid_samples() -> None:
    result = audit_settled_count_contention([_sample(), _sample(duration_ms=100)])
    validate_contention_audit(result)
    assert result.status == "CLEAR"
    assert result.sample_count == 2
    assert result.execution_authorized is False


def test_empty_input_and_sample_bound_fail_closed() -> None:
    with pytest.raises(SettledCountContentionAuditError, match="AUDIT_INPUT_EMPTY"):
        audit_settled_count_contention([])
    with pytest.raises(SettledCountContentionAuditError, match="SAMPLE_BOUND_EXCEEDED"):
        audit_settled_count_contention([_sample(), _sample()], max_samples=1)


def test_exact_latency_and_freshness_boundaries_are_clear() -> None:
    result = audit_settled_count_contention(
        [_sample(age_seconds=300, duration_ms=250)],
        max_age_seconds=300,
        max_duration_ms=250,
    )
    assert result.status == "CLEAR"


def test_latency_busy_and_staleness_classifications() -> None:
    latency = audit_settled_count_contention([_sample(duration_ms=251)])
    assert latency.status == "CONTENDED"
    assert latency.reasons == ("LATENCY_THRESHOLD_EXCEEDED",)
    busy = audit_settled_count_contention([_sample(busy_events=1)])
    assert busy.status == "CONTENDED"
    assert busy.reasons == ("BUSY_EVENTS_OBSERVED",)
    stale = audit_settled_count_contention([_sample(age_seconds=301)])
    assert stale.status == "STALE"
    assert stale.reasons == ("EVIDENCE_STALE",)


def test_invalid_bounds_malformed_and_partial_samples_fail_closed() -> None:
    with pytest.raises(SettledCountContentionAuditError, match="AUDIT_BOUND_INVALID"):
        audit_settled_count_contention([_sample()], max_duration_ms=-1)
    with pytest.raises(SettledCountContentionAuditError, match="SAMPLE_TYPE_INVALID"):
        audit_settled_count_contention([{}])
    with pytest.raises(SettledCountContentionAuditError, match="SAMPLE_FIELD_INVALID"):
        make_query_sample(
            query_fingerprint="q",
            source_identity_hash="a" * 64,
            source_watermark="w",
            age_seconds=0,
            duration_ms=1,
            busy_events=0,
            result_count=-1,
        )


def test_tampering_and_mixed_lineage_fail_closed() -> None:
    sample = _sample()
    with pytest.raises(SettledCountContentionAuditError, match="SAMPLE_HASH_MISMATCH"):
        audit_settled_count_contention([replace(sample, duration_ms=99)])
    with pytest.raises(SettledCountContentionAuditError, match="QUERY_LINEAGE_MIXED"):
        audit_settled_count_contention([sample, _sample(query_fingerprint="other")])
    with pytest.raises(SettledCountContentionAuditError, match="SOURCE_LINEAGE_MIXED"):
        audit_settled_count_contention([sample, _sample(source_watermark="other")])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = audit_settled_count_contention([_sample()])
    with pytest.raises(SettledCountContentionAuditError, match="AUDIT_HASH_MISMATCH"):
        validate_contention_audit(replace(result, audit_hash="0" * 64))
    with pytest.raises(SettledCountContentionAuditError, match="AUDIT_SAFETY_BOUNDARY_INVALID"):
        validate_contention_audit(replace(result, execution_authorized=True))


def test_auditor_has_no_production_mutation_surface() -> None:
    names = set(audit_settled_count_contention.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "replace", "unlink"})


def _sample(
    *,
    query_fingerprint: str = "sha256:settled-count-v1",
    source_watermark: str = "paper_pnl:204",
    age_seconds: int = 1,
    duration_ms: int = 20,
    busy_events: int = 0,
):
    return make_query_sample(
        query_fingerprint=query_fingerprint,
        source_identity_hash="a" * 64,
        source_watermark=source_watermark,
        age_seconds=age_seconds,
        duration_ms=duration_ms,
        busy_events=busy_events,
        result_count=203,
    )
