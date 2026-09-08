from __future__ import annotations

import copy

import pytest

from scripts.local.phase4mp_human_review_workflow import _digest, make_event, validate_workflow

PACKET = "a" * 64
IDENTITY = "b" * 64
PRE = "c" * 64
FIX = "d" * 64
CONSENSUS = "e" * 64


def _append(events, operation, actor, payload, *, epoch=1, occurred_at=None):
    sequence = len(events) + 1
    events.append(
        make_event(
            operation,
            workflow_id="workflow-1",
            review_epoch=epoch,
            sequence=sequence,
            actor_id=actor,
            occurred_at=occurred_at or f"2026-08-29T00:{sequence:02d}:00Z",
            packet_sha256=PACKET,
            implementation_identity_sha256=IDENTITY,
            payload=payload,
            previous_event_sha256=events[-1]["event_sha256"] if events else "0" * 64,
        )
    )
    return events


def _workflow():
    rows = _append(
        [],
        "INTAKE",
        "requester",
        {
            "requester_id": "requester",
            "pre_fix_evidence_sha256": PRE,
            "review_expires_at": "2026-08-30T00:00:00Z",
        },
    )
    _append(rows, "ASSIGN_REVIEWERS", "coordinator", {"reviewers": ["reviewer-a", "reviewer-b"]})
    _append(rows, "INSPECT_EVIDENCE", "reviewer-a", {"pre_fix_evidence_sha256": PRE})
    _append(rows, "SELECT_FIX", "reviewer-b", {"selection": "FIX_PRIMARY"})
    _append(
        rows,
        "ATTACH_FIX_EVIDENCE",
        "implementer",
        {"selection": "FIX_PRIMARY", "fix_evidence_sha256": FIX},
    )
    _append(
        rows,
        "REVERIFY_PACKAGE",
        "verifier",
        {"fix_evidence_sha256": FIX, "package_verdict": "PASS", "verification_sha256": "f" * 64},
    )
    _append(
        rows,
        "RERUN_VERIFIERS",
        "verifier",
        {
            "primary_verdict": "PASS",
            "secondary_verdict": "PASS",
            "first_differing_semantic_field": None,
            "implementation_identity_sha256": IDENTITY,
            "consensus_sha256": CONSENSUS,
        },
    )
    context = _digest(
        {
            "packet_sha256": PACKET,
            "fix_evidence_sha256": FIX,
            "post_fix_consensus_sha256": CONSENSUS,
            "review_epoch": 1,
        }
    )
    _append(rows, "SIGN_OFF", "reviewer-a", {"closure_context_sha256": context})
    _append(rows, "SIGN_OFF", "reviewer-b", {"closure_context_sha256": context})
    _append(rows, "CLOSE", "coordinator", {"post_fix_consensus_sha256": CONSENSUS})
    return rows


def _validate(events=None, **kwargs):
    return validate_workflow(
        events or _workflow(),
        expected_packet_sha256=PACKET,
        expected_implementation_identity_sha256=IDENTITY,
        evaluated_at=kwargs.get("evaluated_at", "2026-08-29T01:00:00Z"),
    )


def _rehash_chain(rows, start):
    previous = rows[start - 1]["event_sha256"] if start else "0" * 64
    for row in rows[start:]:
        row["previous_event_sha256"] = previous
        row["event_sha256"] = _digest(
            {key: value for key, value in row.items() if key != "event_sha256"}
        )
        previous = row["event_sha256"]


def test_complete_lifecycle_closes_deterministically_without_acceptance() -> None:
    first = _validate()
    assert first == _validate()
    assert first["verdict"] == "PASS"
    assert first["state"] == "CLOSED"
    closure = first["closure_certificate"]
    assert closure["disagreement_resolved"] is True
    assert closure["package_acceptance_authorized"] is False
    assert closure["repair_execution_authorized"] is False
    assert closure["order_capability"] is False


@pytest.mark.parametrize(
    "mutation,error",
    [
        (
            lambda rows: rows[1]["payload"].update(reviewers=["requester", "reviewer-a"]),
            "SELF_OR_DUPLICATE_REVIEWER",
        ),
        (
            lambda rows: rows[1]["payload"].update(reviewers=["reviewer-a", "reviewer-a"]),
            "SELF_OR_DUPLICATE_REVIEWER",
        ),
        (
            lambda rows: rows[2]["payload"].update(pre_fix_evidence_sha256="f" * 64),
            "PRE_FIX_EVIDENCE_MUTATED",
        ),
        (
            lambda rows: rows[4]["payload"].update(fix_evidence_sha256="not-a-hash"),
            "FIX_EVIDENCE_STALE_OR_UNBOUND",
        ),
        (
            lambda rows: rows[6]["payload"].update(secondary_verdict="REFUSE"),
            "POST_FIX_CONSENSUS_INVALID",
        ),
        (lambda rows: rows[0].update(packet_sha256="f" * 64), "PACKET_SUBSTITUTION"),
        (lambda rows: rows[7].update(actor_id="reviewer-b"), "REPLAYED_SIGNOFF"),
    ],
)
def test_identity_evidence_consensus_and_review_mutations_fail(mutation, error: str) -> None:
    rows = _workflow()
    mutation(rows)
    _rehash_chain(rows, 0)
    result = _validate(rows)
    assert result["verdict"] == "REFUSE"
    assert any(error in value for value in result["errors"])


def test_skipped_state_and_replayed_signoff_fail_closed() -> None:
    rows = _workflow()
    rows.pop(2)
    for index, row in enumerate(rows):
        row["sequence"] = index + 1
    _rehash_chain(rows, 0)
    assert any("TRANSITION" in error for error in _validate(rows)["errors"])
    rows = _workflow()
    rows.insert(8, copy.deepcopy(rows[7]))
    assert any("REPLAY" in error for error in _validate(rows)["errors"])


def test_post_closure_mutation_fails() -> None:
    rows = _workflow()
    _append(rows, "WITHDRAW", "requester", {})
    assert any("POST_TERMINAL_MUTATION" in error for error in _validate(rows)["errors"])


def test_expired_open_review_fails_and_expire_transition_closes() -> None:
    open_rows = _workflow()[:3]
    result = _validate(open_rows, evaluated_at="2026-08-30T00:00:00Z")
    assert "REVIEW_EXPIRED_UNCLOSED" in result["errors"]
    _append(open_rows, "EXPIRE", "coordinator", {}, occurred_at="2026-08-30T00:00:00Z")
    assert _validate(open_rows, evaluated_at="2026-08-30T00:00:00Z")["state"] == "EXPIRED"


def test_reopen_requires_new_epoch_and_new_evidence() -> None:
    rows = _workflow()
    _append(rows, "REOPEN", "coordinator", {"new_evidence_sha256": "1" * 64}, epoch=1)
    assert any("REOPEN_REQUIRES_NEW_EPOCH" in error for error in _validate(rows)["errors"])
    rows = _workflow()
    _append(rows, "REOPEN", "coordinator", {"new_evidence_sha256": "1" * 64}, epoch=2)
    result = _validate(rows)
    assert result["verdict"] == "PASS"
    assert result["state"] == "INTAKEN"
    assert result["review_epoch"] == 2
    assert result["closure_certificate"] is None


def test_simulator_has_no_persistence_acceptance_execution_or_order_capability() -> None:
    safety = _validate()["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
