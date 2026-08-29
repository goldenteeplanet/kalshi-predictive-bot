from __future__ import annotations

import copy

import pytest

from scripts.local.phase4mb_checkpoint_repair_planner import plan_repair
from scripts.local.phase4mc_repair_plan_mutation_audit import (
    SAFETY,
    _body_hash,
    audit_candidate,
    plan_errors,
    run_mutation_audit,
)
from tests.test_phase4mb_checkpoint_repair_planner import _checkpoints, _manifest


def _repair_plan():
    return plan_repair(
        "1" * 64,
        8,
        _checkpoints()[:6],
        _manifest(),
        evaluated_at="2026-08-28T20:10:00Z",
    )


def _rehash(plan):
    plan["plan_sha256"] = _body_hash(plan)


def test_valid_baseline_and_identical_candidate_pass_deterministically() -> None:
    plan = _repair_plan()
    assert plan_errors(plan) == []
    assert audit_candidate(plan, copy.deepcopy(plan)) == audit_candidate(plan, copy.deepcopy(plan))
    assert audit_candidate(plan, copy.deepcopy(plan))["verdict"] == "PASS"


def test_complete_mutation_corpus_is_rejected() -> None:
    result = run_mutation_audit(_repair_plan())
    assert result["verdict"] == "PASS"
    assert result["mutation_count"] >= 25
    assert result["mutation_count"] == result["rejected_count"]
    assert all(row["errors"] for row in result["records"])


@pytest.mark.parametrize("field", sorted(SAFETY - {"planning_only"}))
def test_every_operational_capability_is_rejected_even_after_rehash(field: str) -> None:
    baseline = _repair_plan()
    candidate = copy.deepcopy(baseline)
    candidate["safety"][field] = True
    _rehash(candidate)
    result = audit_candidate(baseline, candidate)
    assert result["verdict"] == "REFUSE"
    assert "CAPABILITY_ESCALATION" in result["errors"]


def test_trusted_prefix_cannot_expand_even_with_forged_consistent_hash() -> None:
    baseline = _repair_plan()
    candidate = copy.deepcopy(baseline)
    candidate["trusted_prefix_count"] += 1
    _rehash(candidate)
    result = audit_candidate(baseline, candidate)
    assert result["verdict"] == "REFUSE"
    assert "TRUST_PREFIX_EXPANDED" in result["errors"]


@pytest.mark.parametrize(
    "action_name",
    ["REACQUIRE_EVIDENCE", "REPLAY_FROM_CHECKPOINT", "REBUILD_RESUME_TOKEN"],
)
def test_mandatory_action_suppression_is_rejected(action_name: str) -> None:
    baseline = _repair_plan()
    candidate = copy.deepcopy(baseline)
    candidate["actions"] = [row for row in candidate["actions"] if row["action"] != action_name]
    candidate["minimality"]["action_count"] = len(candidate["actions"])
    _rehash(candidate)
    result = audit_candidate(baseline, candidate)
    assert result["verdict"] == "REFUSE"
    assert any("ACTIONS_MISMATCH" in error for error in result["errors"])


def test_revalidation_suppression_and_hash_invention_are_rejected() -> None:
    baseline = _repair_plan()
    for field, value in (("revalidate_invariants", False), ("checkpoint_sha256", "f" * 64)):
        candidate = copy.deepcopy(baseline)
        candidate["actions"][-1][field] = value
        _rehash(candidate)
        assert audit_candidate(baseline, candidate)["verdict"] == "REFUSE"


def test_source_edit_and_hash_invention_authorizations_are_rejected() -> None:
    baseline = _repair_plan()
    for field in ("source_edit_authorized", "hash_invention_authorized"):
        candidate = copy.deepcopy(baseline)
        candidate["minimality"][field] = True
        _rehash(candidate)
        assert audit_candidate(baseline, candidate)["verdict"] == "REFUSE"


def test_invalid_baseline_fails_closed_without_running_mutations() -> None:
    baseline = _repair_plan()
    baseline["safety"]["order_capability"] = True
    _rehash(baseline)
    result = run_mutation_audit(baseline)
    assert result["verdict"] == "REFUSE"
    assert result["mutation_count"] == 0
    assert "CAPABILITY_ESCALATION" in result["baseline_errors"]


def test_auditor_itself_exposes_no_operational_capability() -> None:
    safety = run_mutation_audit(_repair_plan())["safety"]
    assert safety["read_only"] is True
    assert all(value is False for key, value in safety.items() if key != "read_only")
