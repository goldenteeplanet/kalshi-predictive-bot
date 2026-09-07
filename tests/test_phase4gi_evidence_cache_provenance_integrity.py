from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.phase4cd.evidence_cache_memory_bounds import (
    evaluate_evidence_cache_memory_bounds,
    make_cache_entry_descriptor,
)
from kalshi_predictor.phase4cd.evidence_cache_provenance_integrity import (
    EvidenceCacheProvenanceIntegrityError,
    make_cache_provenance_record,
    validate_cache_provenance_integrity,
    verify_cache_provenance_integrity,
)
from kalshi_predictor.phase4cd.evidence_cache_single_flight_proposal import (
    propose_evidence_cache_single_flight,
)
from kalshi_predictor.phase4cd.evidence_cache_stampede_prevention import (
    make_cache_snapshot,
    prevent_evidence_cache_stampede,
)
from kalshi_predictor.phase4cd.evidence_query_cancellation_boundaries import (
    build_query_cancellation_boundaries,
)
from kalshi_predictor.phase4cd.evidence_query_deadline_propagation import propagate_query_deadline
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


def test_valid_provenance_chain_is_intact() -> None:
    bounds, records = _inputs()
    result = verify_cache_provenance_integrity(memory_bounds=bounds, records=records)
    validate_cache_provenance_integrity(result)
    assert result.status == "INTACT"
    assert result.record_count == 2
    assert result.execution_authorized is False


def test_empty_partial_and_record_bound_fail_closed() -> None:
    bounds, records = _inputs()
    with pytest.raises(EvidenceCacheProvenanceIntegrityError, match="RECORDS_EMPTY"):
        verify_cache_provenance_integrity(memory_bounds=bounds, records=[])
    with pytest.raises(EvidenceCacheProvenanceIntegrityError, match="MEMORY_BOUNDS_INPUT_INVALID"):
        verify_cache_provenance_integrity(memory_bounds=None, records=records)
    with pytest.raises(EvidenceCacheProvenanceIntegrityError, match="RECORD_BOUND_EXCEEDED"):
        verify_cache_provenance_integrity(memory_bounds=bounds, records=records, max_records=1)


def test_exact_freshness_boundary_is_intact() -> None:
    bounds, records = _inputs(age=300, bounds_max_age=300)
    result = verify_cache_provenance_integrity(
        memory_bounds=bounds, records=records, max_evidence_age_seconds=300
    )
    assert result.status == "INTACT"


def test_stale_provenance_is_explicit() -> None:
    bounds, records = _inputs(age=301, bounds_max_age=301)
    result = verify_cache_provenance_integrity(
        memory_bounds=bounds, records=records, max_evidence_age_seconds=300
    )
    assert result.status == "STALE"
    assert result.reasons == ("PROVENANCE_EVIDENCE_STALE",)


def test_malformed_partial_tampered_and_broken_chain_fail_closed() -> None:
    bounds, records = _inputs()
    with pytest.raises(EvidenceCacheProvenanceIntegrityError, match="RECORD_TYPE_INVALID"):
        verify_cache_provenance_integrity(memory_bounds=bounds, records=[{}, records[1]])
    with pytest.raises(EvidenceCacheProvenanceIntegrityError, match="RECORD_HASH_MISMATCH"):
        verify_cache_provenance_integrity(
            memory_bounds=bounds, records=[replace(records[0], content_hash="bad"), records[1]]
        )
    broken = make_cache_provenance_record(
        descriptor=records[1].descriptor, content_hash="p2", previous_record_hash=None
    )
    with pytest.raises(EvidenceCacheProvenanceIntegrityError, match="PROVENANCE_CHAIN_BROKEN"):
        verify_cache_provenance_integrity(memory_bounds=bounds, records=[records[0], broken])


def test_entry_manifest_and_count_mismatch_fail_closed() -> None:
    bounds, records = _inputs()
    with pytest.raises(EvidenceCacheProvenanceIntegrityError, match="RECORD_COUNT_MISMATCH"):
        verify_cache_provenance_integrity(memory_bounds=bounds, records=records[:1])
    other = make_cache_entry_descriptor(
        cache_key_hash="x" * 64,
        source_identity_hash="a" * 64,
        source_watermark="w",
        size_bytes=10,
        age_seconds=1,
    )
    replacement = make_cache_provenance_record(
        descriptor=other, content_hash="px", previous_record_hash=records[0].record_hash
    )
    with pytest.raises(EvidenceCacheProvenanceIntegrityError, match="ENTRY_MANIFEST_MISMATCH"):
        verify_cache_provenance_integrity(memory_bounds=bounds, records=[records[0], replacement])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    bounds, records = _inputs()
    result = verify_cache_provenance_integrity(memory_bounds=bounds, records=records)
    with pytest.raises(EvidenceCacheProvenanceIntegrityError, match="INTEGRITY_HASH_MISMATCH"):
        validate_cache_provenance_integrity(replace(result, integrity_hash="0" * 64))
    with pytest.raises(
        EvidenceCacheProvenanceIntegrityError, match="INTEGRITY_SAFETY_BOUNDARY_INVALID"
    ):
        validate_cache_provenance_integrity(replace(result, execution_authorized=True))


def test_verifier_has_no_cache_query_or_mutation_surface() -> None:
    names = set(verify_cache_provenance_integrity.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "delete", "execute", "open", "replace", "unlink"})


def _proposal():
    sample = make_query_sample(
        query_fingerprint="q",
        source_identity_hash="a" * 64,
        source_watermark="w",
        age_seconds=1,
        duration_ms=1,
        busy_events=0,
        result_count=1,
    )
    audit = audit_settled_count_contention([sample])
    budget = build_sqlite_read_transaction_budget(
        audit=audit, requested_rows=1, requested_duration_ms=100
    )
    observation = make_busy_timeout_observation(
        source_identity_hash="a" * 64, source_watermark="w", age_seconds=1, configured_timeout_ms=0
    )
    timeout = build_busy_timeout_evidence(budget=budget, observations=[observation])
    boundary = build_query_cancellation_boundaries(
        budget=budget, timeout_evidence=timeout, cancel_after_ms=100, progress_check_interval_ms=10
    )
    propagation = propagate_query_deadline(
        boundaries=boundary, stages=["cache"], boundary_age_seconds=1, elapsed_ms=1
    )
    snapshot = make_cache_snapshot(
        cache_key_hash="c" * 64,
        source_identity_hash="a" * 64,
        source_watermark="w",
        cache_age_seconds=61,
        refresh_inflight=False,
        refresh_lease_age_seconds=None,
    )
    policy = prevent_evidence_cache_stampede(
        propagation=propagation, snapshot=snapshot, requester_ids=["worker"]
    )
    return propose_evidence_cache_single_flight(
        policy=policy,
        propagation=propagation,
        requester_ids=["worker"],
        policy_age_seconds=1,
        follower_wait_ms=1,
    )


def _inputs(*, age: int = 1, bounds_max_age: int = 300):
    descriptors = [
        make_cache_entry_descriptor(
            cache_key_hash=key * 64,
            source_identity_hash="a" * 64,
            source_watermark="w",
            size_bytes=10,
            age_seconds=age,
        )
        for key in ("1", "2")
    ]
    bounds = evaluate_evidence_cache_memory_bounds(
        proposal=_proposal(), entries=descriptors, max_evidence_age_seconds=bounds_max_age
    )
    first = make_cache_provenance_record(
        descriptor=descriptors[0], content_hash="p1", previous_record_hash=None
    )
    second = make_cache_provenance_record(
        descriptor=descriptors[1], content_hash="p2", previous_record_hash=first.record_hash
    )
    return bounds, [first, second]
