from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.phase4cd.evidence_cache_cold_start_seeding import (
    EvidenceCacheColdStartSeedError,
    make_cache_seed_candidate,
    propose_cache_cold_start_seed,
    validate_cache_cold_start_seed_proposal,
)
from kalshi_predictor.phase4cd.evidence_cache_memory_bounds import (
    evaluate_evidence_cache_memory_bounds,
    make_cache_entry_descriptor,
)
from kalshi_predictor.phase4cd.evidence_cache_provenance_integrity import (
    make_cache_provenance_record,
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


def test_valid_seed_proposal_is_deterministic_and_bounded() -> None:
    first = _proposal(candidates=[_candidate("1", priority=1), _candidate("2", priority=2)])
    second = _proposal(candidates=[_candidate("2", priority=2), _candidate("1", priority=1)])
    validate_cache_cold_start_seed_proposal(first)
    assert first.decision == "PROPOSE"
    assert [item.cache_key_hash for item in first.selected_items] == [
        item.cache_key_hash for item in second.selected_items
    ]
    assert first.execution_authorized is False


def test_empty_partial_and_candidate_bound_fail_closed() -> None:
    with pytest.raises(EvidenceCacheColdStartSeedError, match="CANDIDATES_EMPTY"):
        _proposal(candidates=[])
    with pytest.raises(EvidenceCacheColdStartSeedError, match="INTEGRITY_INPUT_INVALID"):
        propose_cache_cold_start_seed(integrity=None, candidates=[_candidate("1")])
    with pytest.raises(EvidenceCacheColdStartSeedError, match="CANDIDATE_BOUND_EXCEEDED"):
        _proposal(candidates=[_candidate("1"), _candidate("2")], max_candidates=1)


def test_exact_age_count_and_byte_boundaries_propose() -> None:
    result = _proposal(
        candidates=[_candidate("1", age=300, size=50), _candidate("2", size=50)],
        max_seed_entries=2,
        max_seed_bytes=100,
    )
    assert result.decision == "PROPOSE"
    assert result.selected_count == 2
    assert result.selected_total_bytes == 100


def test_stale_evidence_and_no_fitting_candidate_reject() -> None:
    stale = _proposal(candidates=[_candidate("1", age=301)])
    assert stale.decision == "REJECT"
    assert stale.reasons == ("SEED_EVIDENCE_STALE",)
    oversized = _proposal(candidates=[_candidate("1", size=101)], max_seed_bytes=100)
    assert oversized.decision == "REJECT"
    assert oversized.reasons == ("NO_SEED_CANDIDATE_FITS",)


def test_malformed_duplicate_tampered_and_lineage_inputs_fail_closed() -> None:
    with pytest.raises(EvidenceCacheColdStartSeedError, match="CANDIDATE_FIELD_INVALID"):
        _candidate("1", size=-1)
    with pytest.raises(EvidenceCacheColdStartSeedError, match="CANDIDATE_KEY_DUPLICATE"):
        _proposal(candidates=[_candidate("1"), _candidate("1")])
    candidate = _candidate("1")
    with pytest.raises(EvidenceCacheColdStartSeedError, match="CANDIDATE_HASH_MISMATCH"):
        _proposal(candidates=[replace(candidate, size_bytes=99)])
    with pytest.raises(EvidenceCacheColdStartSeedError, match="SOURCE_WATERMARK_MISMATCH"):
        _proposal(candidates=[_candidate("1", watermark="other")])


def test_count_limit_omits_lower_priority_candidates() -> None:
    result = _proposal(
        candidates=[_candidate("1", priority=1), _candidate("2", priority=2)], max_seed_entries=1
    )
    assert result.selected_count == 1
    assert result.omitted_count == 1
    assert result.selected_items[0].priority == 2


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    proposal = _proposal(candidates=[_candidate("1")])
    with pytest.raises(EvidenceCacheColdStartSeedError, match="SEED_HASH_MISMATCH"):
        validate_cache_cold_start_seed_proposal(replace(proposal, proposal_hash="0" * 64))
    with pytest.raises(EvidenceCacheColdStartSeedError, match="SEED_SAFETY_BOUNDARY_INVALID"):
        validate_cache_cold_start_seed_proposal(replace(proposal, execution_authorized=True))


def test_proposer_has_no_cache_query_or_mutation_surface() -> None:
    names = set(propose_cache_cold_start_seed.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "execute", "open", "populate", "replace", "unlink"}
    )


def _integrity():
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
    flight = propose_evidence_cache_single_flight(
        policy=policy,
        propagation=propagation,
        requester_ids=["worker"],
        policy_age_seconds=1,
        follower_wait_ms=1,
    )
    descriptor = make_cache_entry_descriptor(
        cache_key_hash="e" * 64,
        source_identity_hash="a" * 64,
        source_watermark="w",
        size_bytes=10,
        age_seconds=1,
    )
    bounds = evaluate_evidence_cache_memory_bounds(proposal=flight, entries=[descriptor])
    record = make_cache_provenance_record(
        descriptor=descriptor, content_hash="payload", previous_record_hash=None
    )
    return verify_cache_provenance_integrity(memory_bounds=bounds, records=[record])


def _candidate(key: str, *, age: int = 1, size: int = 10, priority: int = 1, watermark: str = "w"):
    return make_cache_seed_candidate(
        cache_key_hash=key * 64,
        content_hash="p" + key,
        source_identity_hash="a" * 64,
        source_watermark=watermark,
        evidence_age_seconds=age,
        size_bytes=size,
        priority=priority,
    )


def _proposal(*, candidates, **kwargs):
    return propose_cache_cold_start_seed(integrity=_integrity(), candidates=candidates, **kwargs)
