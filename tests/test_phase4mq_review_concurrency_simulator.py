from __future__ import annotations

import copy

from scripts.local.phase4mp_human_review_workflow import make_event
from scripts.local.phase4mq_review_concurrency_simulator import (
    audit_interleavings,
    crash_recovery_matrix,
    make_proposal,
    simulate_cas,
)
from tests.test_phase4mp_human_review_workflow import IDENTITY, PACKET, _workflow


def _event(base, operation, actor, payload, *, epoch=1, occurred_at="2026-08-29T00:20:00Z"):
    return make_event(
        operation,
        workflow_id="workflow-1",
        review_epoch=epoch,
        sequence=len(base) + 1,
        actor_id=actor,
        occurred_at=occurred_at,
        packet_sha256=PACKET,
        implementation_identity_sha256=IDENTITY,
        payload=payload,
        previous_event_sha256=base[-1]["event_sha256"] if base else "0" * 64,
    )


def _proposal(base, event, **kwargs):
    return make_proposal(
        event,
        expected_generation=len(base),
        expected_head_sha256=base[-1]["event_sha256"] if base else "0" * 64,
        **kwargs,
    )


def _simulate(base, proposals):
    return simulate_cas(
        base,
        proposals,
        expected_packet_sha256=PACKET,
        expected_implementation_identity_sha256=IDENTITY,
        evaluated_at="2026-08-29T01:00:00Z",
    )


def test_conflicting_assignments_have_at_most_one_winner() -> None:
    base = _workflow()[:1]
    first = _proposal(
        base,
        _event(
            base, "ASSIGN_REVIEWERS", "coordinator", {"reviewers": ["reviewer-a", "reviewer-b"]}
        ),
    )
    second = _proposal(
        base,
        _event(
            base, "ASSIGN_REVIEWERS", "coordinator", {"reviewers": ["reviewer-c", "reviewer-d"]}
        ),
    )
    result = _simulate(base, [first, second])
    assert result["verdict"] == "PASS"
    assert result["accepted_count"] == 1
    assert [row["reason"] for row in result["outcomes"]] == [
        "LINEARIZED_AT_EXACT_CAS_POINT",
        "CAS_GENERATION_OR_HEAD_MISMATCH",
    ]


def test_exact_retry_is_idempotent_and_conflicting_retry_refuses() -> None:
    base = _workflow()[:1]
    event = _event(
        base, "ASSIGN_REVIEWERS", "coordinator", {"reviewers": ["reviewer-a", "reviewer-b"]}
    )
    proposal = _proposal(base, event)
    result = _simulate(base, [proposal, copy.deepcopy(proposal)])
    assert result["accepted_count"] == 1
    assert result["outcomes"][1]["outcome"] == "IDEMPOTENT_RETRY"
    conflict = copy.deepcopy(proposal)
    conflict["event"]["actor_id"] = "other"
    result = _simulate(base, [proposal, conflict])
    assert result["outcomes"][1]["reason"] == "CONFLICTING_RETRY"


def test_prepared_event_recovers_fail_closed_without_append() -> None:
    base = _workflow()[:1]
    event = _event(
        base, "ASSIGN_REVIEWERS", "coordinator", {"reviewers": ["reviewer-a", "reviewer-b"]}
    )
    result = _simulate(base, [_proposal(base, event, durability="PREPARED")])
    assert result["accepted_count"] == 0
    assert result["final_generation"] == len(base)
    assert result["outcomes"][0]["outcome"] == "RECOVERY_DISCARD"


def test_simultaneous_fix_selection_and_evidence_have_one_winner() -> None:
    selection_base = _workflow()[:3]
    selections = [
        _proposal(
            selection_base, _event(selection_base, "SELECT_FIX", "reviewer-a", {"selection": value})
        )
        for value in ("FIX_PRIMARY", "FIX_BOTH")
    ]
    assert _simulate(selection_base, selections)["accepted_count"] == 1
    evidence_base = _workflow()[:4]
    evidence = [
        _proposal(
            evidence_base,
            _event(
                evidence_base,
                "ATTACH_FIX_EVIDENCE",
                "implementer",
                {"selection": "FIX_PRIMARY", "fix_evidence_sha256": value * 64},
            ),
        )
        for value in ("d", "f")
    ]
    assert _simulate(evidence_base, evidence)["accepted_count"] == 1


def test_crossing_signoffs_require_rebase_to_new_head() -> None:
    base = _workflow()[:7]
    context = _workflow()[7]["payload"]
    proposals = [
        _proposal(base, _event(base, "SIGN_OFF", actor, context))
        for actor in ("reviewer-a", "reviewer-b")
    ]
    first = _simulate(base, proposals)
    assert first["accepted_count"] == 1
    rebased_base = first["durable_events"]
    rebased = _proposal(
        rebased_base,
        _event(rebased_base, "SIGN_OFF", "reviewer-b", context, occurred_at="2026-08-29T00:21:00Z"),
    )
    second = _simulate(rebased_base, [rebased])
    assert second["accepted_count"] == 1
    assert second["final_workflow_state"] == "SIGNED"


def test_close_withdraw_and_close_expire_races_linearize_once() -> None:
    base = _workflow()[:9]
    close = _proposal(
        base,
        _event(base, "CLOSE", "coordinator", {"post_fix_consensus_sha256": "e" * 64}),
    )
    withdraw = _proposal(base, _event(base, "WITHDRAW", "requester", {}))
    first = _simulate(base, [close, withdraw])
    assert first["accepted_count"] == 1
    assert first["final_workflow_state"] == "CLOSED"
    expire = _proposal(base, _event(base, "EXPIRE", "coordinator", {}))
    second = _simulate(base, [expire, close])
    assert second["accepted_count"] == 1
    assert second["final_workflow_state"] == "EXPIRED"


def test_reopen_race_requires_new_epoch_and_rejects_postclosure_mutation() -> None:
    base = _workflow()
    reopen = _proposal(
        base,
        _event(base, "REOPEN", "coordinator", {"new_evidence_sha256": "1" * 64}, epoch=2),
    )
    mutation = _proposal(base, _event(base, "WITHDRAW", "requester", {}))
    result = _simulate(base, [reopen, mutation])
    assert result["accepted_count"] == 1
    assert result["final_workflow_state"] == "INTAKEN"
    stale = _proposal(
        base,
        _event(base, "REOPEN", "coordinator", {"new_evidence_sha256": "2" * 64}, epoch=1),
    )
    assert _simulate(base, [stale])["accepted_count"] == 0


def test_crash_before_and_after_every_append_preserves_durable_prefix() -> None:
    events = _workflow()
    result = crash_recovery_matrix(
        events,
        expected_packet_sha256=PACKET,
        expected_implementation_identity_sha256=IDENTITY,
        evaluated_at="2026-08-29T01:00:00Z",
    )
    assert result["verdict"] == "PASS"
    assert result["crash_point_count"] == len(events) + 1
    assert all(row["durable_events_lost"] == 0 for row in result["records"])
    assert all(row["resume_from_exact_head"] is True for row in result["records"])


def test_interleaving_audit_and_safety_are_deterministic() -> None:
    base = _workflow()[:1]
    proposals = [
        _proposal(
            base,
            _event(base, "ASSIGN_REVIEWERS", "coordinator", {"reviewers": reviewers}),
        )
        for reviewers in (["reviewer-a", "reviewer-b"], ["reviewer-c", "reviewer-d"])
    ]
    scenarios = [{"name": "assignment-race", "base_events": base, "proposals": proposals}]
    first = audit_interleavings(
        scenarios,
        expected_packet_sha256=PACKET,
        expected_implementation_identity_sha256=IDENTITY,
        evaluated_at="2026-08-29T01:00:00Z",
    )
    assert first == audit_interleavings(
        scenarios,
        expected_packet_sha256=PACKET,
        expected_implementation_identity_sha256=IDENTITY,
        evaluated_at="2026-08-29T01:00:00Z",
    )
    assert first["verdict"] == "PASS"
    safety = _simulate(base, proposals)["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
