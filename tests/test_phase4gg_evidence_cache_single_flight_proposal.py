from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.phase4cd.evidence_cache_single_flight_proposal import (
    EvidenceCacheSingleFlightError,
    propose_evidence_cache_single_flight,
    validate_single_flight_proposal,
)
from kalshi_predictor.phase4cd.evidence_cache_stampede_prevention import (
    make_cache_snapshot,
    prevent_evidence_cache_stampede,
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


def test_stale_cache_proposes_exactly_one_leader() -> None:
    proposal = _proposal(cache_age=61)
    validate_single_flight_proposal(proposal)
    assert proposal.decision == "READY"
    assert proposal.leader_count == 1
    assert proposal.execution_authorized is False


def test_empty_partial_and_participant_bounds_fail_closed() -> None:
    propagation, policy = _inputs()
    with pytest.raises(EvidenceCacheSingleFlightError, match="PARTICIPANTS_EMPTY"):
        _proposal(requesters=[])
    with pytest.raises(EvidenceCacheSingleFlightError, match="PROPOSAL_INPUT_INVALID"):
        propose_evidence_cache_single_flight(
            policy=None,
            propagation=propagation,
            requester_ids=["a"],
            policy_age_seconds=1,
            follower_wait_ms=1,
        )
    with pytest.raises(EvidenceCacheSingleFlightError, match="PARTICIPANT_BOUND_EXCEEDED"):
        propose_evidence_cache_single_flight(
            policy=policy,
            propagation=propagation,
            requester_ids=["a", "b"],
            policy_age_seconds=1,
            follower_wait_ms=1,
            max_participants=1,
        )


def test_exact_age_and_wait_boundaries_are_ready() -> None:
    propagation, policy = _inputs(cache_age=61)
    proposal = propose_evidence_cache_single_flight(
        policy=policy,
        propagation=propagation,
        requester_ids=["worker-a", "worker-b"],
        policy_age_seconds=60,
        max_policy_age_seconds=60,
        follower_wait_ms=propagation.remaining_ms,
    )
    assert proposal.decision == "READY"


def test_stale_policy_and_overlong_wait_reject() -> None:
    stale = _proposal(cache_age=61, policy_age=61)
    assert stale.decision == "REJECT"
    assert stale.reasons == ("POLICY_STALE",)
    propagation, policy = _inputs(cache_age=61)
    wait = propose_evidence_cache_single_flight(
        policy=policy,
        propagation=propagation,
        requester_ids=["worker-a", "worker-b"],
        policy_age_seconds=1,
        follower_wait_ms=propagation.remaining_ms + 1,
    )
    assert wait.decision == "REJECT"
    assert wait.reasons == ("FOLLOWER_WAIT_EXCEEDS_DEADLINE",)


def test_fresh_and_inflight_roles_are_complete() -> None:
    fresh = _proposal(cache_age=1)
    assert {item.role for item in fresh.participants} == {"CACHE_READER"}
    waiting = _proposal(cache_age=61, inflight=True, lease_age=30)
    assert {item.role for item in waiting.participants} == {"FOLLOWER"}


def test_malformed_duplicate_and_count_mismatch_fail_closed() -> None:
    with pytest.raises(EvidenceCacheSingleFlightError, match="PROPOSAL_FIELD_INVALID"):
        _proposal(policy_age=-1)
    with pytest.raises(EvidenceCacheSingleFlightError, match="PARTICIPANT_DUPLICATE"):
        _proposal(requesters=["a", "a"])
    propagation, policy = _inputs(cache_age=61)
    with pytest.raises(EvidenceCacheSingleFlightError, match="PARTICIPANT_COUNT_MISMATCH"):
        propose_evidence_cache_single_flight(
            policy=policy,
            propagation=propagation,
            requester_ids=["worker-a"],
            policy_age_seconds=1,
            follower_wait_ms=1,
        )


def test_tampering_and_cross_link_fail_closed() -> None:
    propagation, policy = _inputs(cache_age=61)
    with pytest.raises(EvidenceCacheSingleFlightError, match="PROPOSAL_INPUT_INVALID"):
        propose_evidence_cache_single_flight(
            policy=replace(policy, policy_hash="0" * 64),
            propagation=propagation,
            requester_ids=["worker-a", "worker-b"],
            policy_age_seconds=1,
            follower_wait_ms=1,
        )
    other_propagation, _ = _inputs(watermark="other")
    with pytest.raises(
        EvidenceCacheSingleFlightError, match="POLICY_PROPAGATION_LINK_MISMATCH"
    ):
        propose_evidence_cache_single_flight(
            policy=policy,
            propagation=other_propagation,
            requester_ids=["worker-a", "worker-b"],
            policy_age_seconds=1,
            follower_wait_ms=1,
        )


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    proposal = _proposal(cache_age=61)
    with pytest.raises(EvidenceCacheSingleFlightError, match="PROPOSAL_HASH_MISMATCH"):
        validate_single_flight_proposal(replace(proposal, proposal_hash="0" * 64))
    with pytest.raises(EvidenceCacheSingleFlightError, match="PROPOSAL_SAFETY_BOUNDARY_INVALID"):
        validate_single_flight_proposal(replace(proposal, execution_authorized=True))


def test_proposal_has_no_cache_query_or_mutation_surface() -> None:
    names = set(propose_evidence_cache_single_flight.__code__.co_names)
    assert names.isdisjoint(
        {"acquire", "commit", "connect", "execute", "open", "release", "replace", "unlink"}
    )


def _inputs(
    *,
    cache_age: int = 61,
    inflight: bool = False,
    lease_age: int | None = None,
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
    propagation = propagate_query_deadline(
        boundaries=boundaries,
        stages=["cache", "read"],
        boundary_age_seconds=1,
        elapsed_ms=1,
    )
    snapshot = make_cache_snapshot(
        cache_key_hash="c" * 64,
        source_identity_hash="a" * 64,
        source_watermark=watermark,
        cache_age_seconds=cache_age,
        refresh_inflight=inflight,
        refresh_lease_age_seconds=lease_age,
    )
    policy = prevent_evidence_cache_stampede(
        propagation=propagation,
        snapshot=snapshot,
        requester_ids=["worker-a", "worker-b"],
    )
    return propagation, policy


def _proposal(
    *,
    cache_age: int = 61,
    inflight: bool = False,
    lease_age: int | None = None,
    requesters: list[str] | None = None,
    policy_age: int = 1,
):
    propagation, policy = _inputs(
        cache_age=cache_age, inflight=inflight, lease_age=lease_age
    )
    return propose_evidence_cache_single_flight(
        policy=policy,
        propagation=propagation,
        requester_ids=["worker-a", "worker-b"] if requesters is None else requesters,
        policy_age_seconds=policy_age,
        follower_wait_ms=1,
    )
