from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.phase4cd.evidence_cache_memory_bounds import (
    EvidenceCacheMemoryBoundsError,
    evaluate_evidence_cache_memory_bounds,
    make_cache_entry_descriptor,
    validate_evidence_cache_memory_bounds,
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


def test_valid_entries_are_within_bounds() -> None:
    bounds = _bounds(entries=[_entry("1", size=50), _entry("2", size=50)])
    validate_evidence_cache_memory_bounds(bounds)
    assert bounds.status == "WITHIN_BOUNDS"
    assert bounds.execution_authorized is False


def test_empty_partial_and_input_bound_fail_closed() -> None:
    with pytest.raises(EvidenceCacheMemoryBoundsError, match="ENTRIES_EMPTY"):
        _bounds(entries=[])
    with pytest.raises(EvidenceCacheMemoryBoundsError, match="PROPOSAL_INPUT_INVALID"):
        evaluate_evidence_cache_memory_bounds(proposal=None, entries=[_entry("1")])
    with pytest.raises(EvidenceCacheMemoryBoundsError, match="INPUT_ENTRY_BOUND_EXCEEDED"):
        _bounds(entries=[_entry("1"), _entry("2")], max_input_entries=1)


def test_exact_memory_and_freshness_boundaries_pass() -> None:
    bounds = _bounds(
        entries=[_entry("1", size=50, age=300), _entry("2", size=50)],
        max_entries=2,
        max_total_bytes=100,
        max_entry_bytes=50,
        max_evidence_age_seconds=300,
    )
    assert bounds.status == "WITHIN_BOUNDS"


def test_count_total_and_entry_breaches_produce_deterministic_eviction() -> None:
    entries = [
        _entry("1", size=70, age=1),
        _entry("2", size=40, age=20),
        _entry("3", size=40, age=10),
    ]
    first = _bounds(entries=entries, max_entries=2, max_total_bytes=100, max_entry_bytes=60)
    second = _bounds(
        entries=list(reversed(entries)), max_entries=2, max_total_bytes=100, max_entry_bytes=60
    )
    assert first.status == "EVICTION_REQUIRED"
    assert set(first.reasons) == {
        "ENTRY_BYTES_EXCEEDED",
        "ENTRY_COUNT_EXCEEDED",
        "TOTAL_BYTES_EXCEEDED",
    }
    assert first.eviction_key_hashes == second.eviction_key_hashes
    assert first.eviction_key_hashes


def test_stale_evidence_has_no_eviction_advice() -> None:
    bounds = _bounds(entries=[_entry("1", age=301)], max_evidence_age_seconds=300)
    assert bounds.status == "STALE"
    assert bounds.reasons == ("MEMORY_EVIDENCE_STALE",)
    assert bounds.eviction_key_hashes == ()


def test_malformed_duplicate_tampered_and_lineage_inputs_fail_closed() -> None:
    with pytest.raises(EvidenceCacheMemoryBoundsError, match="ENTRY_FIELD_INVALID"):
        _entry("1", size=-1)
    with pytest.raises(EvidenceCacheMemoryBoundsError, match="ENTRY_KEY_DUPLICATE"):
        _bounds(entries=[_entry("1"), _entry("1")])
    entry = _entry("1")
    with pytest.raises(EvidenceCacheMemoryBoundsError, match="ENTRY_HASH_MISMATCH"):
        _bounds(entries=[replace(entry, size_bytes=99)])
    with pytest.raises(EvidenceCacheMemoryBoundsError, match="SOURCE_WATERMARK_MISMATCH"):
        _bounds(entries=[_entry("1", watermark="other")])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    bounds = _bounds(entries=[_entry("1")])
    with pytest.raises(EvidenceCacheMemoryBoundsError, match="MEMORY_HASH_MISMATCH"):
        validate_evidence_cache_memory_bounds(replace(bounds, bounds_hash="0" * 64))
    with pytest.raises(EvidenceCacheMemoryBoundsError, match="MEMORY_SAFETY_BOUNDARY_INVALID"):
        validate_evidence_cache_memory_bounds(replace(bounds, execution_authorized=True))


def test_evaluator_has_no_cache_query_or_mutation_surface() -> None:
    names = set(evaluate_evidence_cache_memory_bounds.__code__.co_names)
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


def _entry(key: str, *, size: int = 10, age: int = 1, watermark: str = "w"):
    return make_cache_entry_descriptor(
        cache_key_hash=key * 64,
        source_identity_hash="a" * 64,
        source_watermark=watermark,
        size_bytes=size,
        age_seconds=age,
    )


def _bounds(*, entries, **kwargs):
    return evaluate_evidence_cache_memory_bounds(proposal=_proposal(), entries=entries, **kwargs)
