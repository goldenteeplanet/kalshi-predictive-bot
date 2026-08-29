from __future__ import annotations

from scripts.local.phase4nu_byzantine_policy import (
    analyze_policy,
    assess_policy_change,
    compare_policies,
    enumerate_failures,
    scenario_matrix,
)


def test_byzantine_threshold_math_distinguishes_crash_from_byzantine_tolerance() -> None:
    two_of_three = analyze_policy(3, 2, claimed_byzantine_tolerance=1)
    assert two_of_three["verdict"] == "REFUSE"
    assert two_of_three["simultaneous_tolerance"] == 0
    three_of_four = analyze_policy(4, 3, claimed_byzantine_tolerance=1)
    assert three_of_four["verdict"] == "PASS"
    assert three_of_four["safety_tolerance"] == 1
    assert three_of_four["liveness_tolerance"] == 1


def test_bounded_failure_enumeration_is_deterministic_and_classifies_all_outcomes() -> None:
    first = enumerate_failures(4, 3, maximum_faults=2, maximum_cases=128)
    assert first == enumerate_failures(4, 3, maximum_faults=2, maximum_cases=128)
    assert first["verdict"] == "PASS"
    assert {row["classification"] for row in first["cases"]}.issuperset(
        {"BOTH", "SAFETY_ONLY", "NEITHER"}
    )


def test_enumeration_bounds_fail_closed() -> None:
    assert (
        "ENUMERATION_BOUND_INVALID"
        in enumerate_failures(13, 7, maximum_faults=2, maximum_cases=100)["errors"]
    )
    assert (
        "ENUMERATION_BOUND_EXCEEDED"
        in enumerate_failures(8, 5, maximum_faults=5, maximum_cases=5)["errors"]
    )


def test_minority_collusion_cannot_certify_but_quorum_loss_halts_safely() -> None:
    matrix = scenario_matrix(4, 3)
    rows = {row["scenario"]: row for row in matrix["scenarios"]}
    assert rows["ONE_EQUIVOCATING"]["safety"] is True
    assert rows["ONE_EQUIVOCATING"]["liveness"] is True
    assert rows["MINORITY_COLLUSION"]["safety"] is False
    assert rows["CORRELATED_TWO_OFFLINE"]["safety"] is True
    assert rows["CORRELATED_TWO_OFFLINE"]["liveness"] is False
    assert rows["QUORUM_RESTORED"]["classification"] == "BOTH"


def test_key_compromise_revocation_rotation_and_delayed_restoration_are_explicit() -> None:
    rows = {row["scenario"]: row for row in scenario_matrix(4, 3)["scenarios"]}
    assert rows["ONE_COMPROMISED"]["classification"] == "BOTH"
    assert rows["COMPROMISED_REVOKED"]["classification"] == "BOTH"
    assert rows["ROTATION_OVERLAP_LOSS"]["classification"] == "BOTH"
    assert rows["DELAYED_RESTORATION"]["classification"] == "BOTH"


def test_policy_comparison_recommends_valid_deterministic_threshold() -> None:
    policies = [
        {"witness_count": 3, "threshold": 2, "claimed_byzantine_tolerance": 1},
        {"witness_count": 4, "threshold": 3, "claimed_byzantine_tolerance": 1},
        {"witness_count": 5, "threshold": 3, "claimed_byzantine_tolerance": 1},
        {"witness_count": 5, "threshold": 4, "claimed_byzantine_tolerance": 1},
    ]
    result = compare_policies(policies)
    assert result == compare_policies(policies)
    assert result["verdict"] == "PASS"
    assert result["recommended_policy"] == policies[1]
    assert result["residual_risk"]


def test_threshold_weakening_rolls_back_to_prior_policy() -> None:
    current = {"witness_count": 4, "threshold": 3, "claimed_byzantine_tolerance": 1}
    weaker = {"witness_count": 3, "threshold": 2, "claimed_byzantine_tolerance": 1}
    result = assess_policy_change(current, weaker)
    assert result["verdict"] == "REFUSE"
    assert result["rollback_applied"] is True
    assert result["selected_policy"] == current
    stronger = {"witness_count": 7, "threshold": 5, "claimed_byzantine_tolerance": 2}
    assert assess_policy_change(current, stronger)["verdict"] == "PASS"


def test_policy_model_has_no_execution_capability() -> None:
    safety = analyze_policy(4, 3, claimed_byzantine_tolerance=1)["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
