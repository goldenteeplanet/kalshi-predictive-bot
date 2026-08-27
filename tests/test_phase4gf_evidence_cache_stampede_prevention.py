from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.phase4cd.evidence_cache_stampede_prevention import (
    EvidenceCacheStampedeError,
    make_cache_snapshot,
    prevent_evidence_cache_stampede,
    validate_stampede_policy,
)
from kalshi_predictor.phase4cd.evidence_query_cancellation_boundaries import (
    build_query_cancellation_boundaries,
)
from kalshi_predictor.phase4cd.evidence_query_deadline_propagation import (
    propagate_query_deadline,
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


def test_fresh_cache_is_a_hit() -> None:
    policy = _policy(cache_age=60)
    validate_stampede_policy(policy)
    assert policy.decision == "CACHE_HIT"
    assert policy.elected_requester is None
    assert policy.execution_authorized is False


def test_empty_partial_and_requester_bound_fail_closed() -> None:
    with pytest.raises(EvidenceCacheStampedeError, match="REQUESTERS_EMPTY"):
        _policy(requesters=[])
    with pytest.raises(EvidenceCacheStampedeError, match="PROPAGATION_INPUT_INVALID"):
        prevent_evidence_cache_stampede(
            propagation=None, snapshot=_snapshot(), requester_ids=["a"]
        )
    with pytest.raises(EvidenceCacheStampedeError, match="REQUESTER_BOUND_EXCEEDED"):
        _policy(requesters=["a", "b"], max_requesters=1)


def test_exact_cache_and_lease_boundaries() -> None:
    assert _policy(cache_age=60).decision == "CACHE_HIT"
    wait = _policy(cache_age=61, inflight=True, lease_age=30)
    assert wait.decision == "WAIT"
    elected = _policy(cache_age=61, inflight=True, lease_age=31)
    assert elected.decision == "ELECT_ONE"


def test_stale_cache_elects_exactly_one_deterministically() -> None:
    first = _policy(cache_age=61, requesters=["worker-b", "worker-a"])
    second = _policy(cache_age=61, requesters=["worker-a", "worker-b"])
    assert first.decision == "ELECT_ONE"
    assert first.elected_requester == second.elected_requester
    assert first.elected_requester in {"worker-a", "worker-b"}


def test_malformed_snapshot_and_requesters_fail_closed() -> None:
    with pytest.raises(EvidenceCacheStampedeError, match="LEASE_AGE_MISSING"):
        make_cache_snapshot(
            cache_key_hash="c" * 64,
            source_identity_hash="a" * 64,
            source_watermark="paper_pnl:204",
            cache_age_seconds=1,
            refresh_inflight=True,
            refresh_lease_age_seconds=None,
        )
    with pytest.raises(EvidenceCacheStampedeError, match="REQUESTER_DUPLICATE"):
        _policy(requesters=["a", "a"])


def test_tampering_lineage_and_partial_snapshot_fail_closed() -> None:
    snapshot = _snapshot()
    with pytest.raises(EvidenceCacheStampedeError, match="SNAPSHOT_HASH_MISMATCH"):
        prevent_evidence_cache_stampede(
            propagation=_propagation(),
            snapshot=replace(snapshot, cache_age_seconds=99),
            requester_ids=["a"],
        )
    with pytest.raises(EvidenceCacheStampedeError, match="SOURCE_WATERMARK_MISMATCH"):
        prevent_evidence_cache_stampede(
            propagation=_propagation(),
            snapshot=_snapshot(watermark="other"),
            requester_ids=["a"],
        )
    with pytest.raises(EvidenceCacheStampedeError, match="SNAPSHOT_TYPE_INVALID"):
        prevent_evidence_cache_stampede(
            propagation=_propagation(), snapshot={}, requester_ids=["a"]
        )


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    policy = _policy()
    with pytest.raises(EvidenceCacheStampedeError, match="POLICY_HASH_MISMATCH"):
        validate_stampede_policy(replace(policy, policy_hash="0" * 64))
    with pytest.raises(EvidenceCacheStampedeError, match="POLICY_SAFETY_BOUNDARY_INVALID"):
        validate_stampede_policy(replace(policy, execution_authorized=True))


def test_policy_has_no_cache_query_or_mutation_surface() -> None:
    names = set(prevent_evidence_cache_stampede.__code__.co_names)
    assert names.isdisjoint(
        {"acquire", "commit", "connect", "execute", "open", "release", "replace", "unlink"}
    )


def _propagation():
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
    timeout_evidence = build_busy_timeout_evidence(
        budget=budget, observations=[observation]
    )
    boundaries = build_query_cancellation_boundaries(
        budget=budget,
        timeout_evidence=timeout_evidence,
        cancel_after_ms=100,
        progress_check_interval_ms=10,
    )
    return propagate_query_deadline(
        boundaries=boundaries,
        stages=["cache", "read"],
        boundary_age_seconds=1,
        elapsed_ms=1,
    )


def _snapshot(
    *,
    cache_age: int = 1,
    inflight: bool = False,
    lease_age: int | None = None,
    watermark: str = "paper_pnl:204",
):
    return make_cache_snapshot(
        cache_key_hash="c" * 64,
        source_identity_hash="a" * 64,
        source_watermark=watermark,
        cache_age_seconds=cache_age,
        refresh_inflight=inflight,
        refresh_lease_age_seconds=lease_age,
    )


def _policy(
    *,
    cache_age: int = 1,
    inflight: bool = False,
    lease_age: int | None = None,
    requesters: list[str] | None = None,
    max_requesters: int = 32,
):
    return prevent_evidence_cache_stampede(
        propagation=_propagation(),
        snapshot=_snapshot(cache_age=cache_age, inflight=inflight, lease_age=lease_age),
        requester_ids=["worker-a"] if requesters is None else requesters,
        max_requesters=max_requesters,
    )
