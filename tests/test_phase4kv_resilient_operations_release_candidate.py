from dataclasses import replace

import pytest
from kalshi_predictor.workstation.resilient_operations_release_candidate import (
    ResilientOperationsReleaseCandidateError,
    evaluate_resilient_operations_release_candidate,
    make_release_candidate_evidence,
    validate_release_candidate_result,
)


def _evidence(**overrides):
    fields = dict(
        independent_verification_hash="1" * 64,
        operator_drill_hash="2" * 64,
        post_boot_gate_hash="3" * 64,
        rollback_package_hash="4" * 64,
        covered_phase_count=98,
        cumulative_tests_passed=694,
        independent_verification_passed=True,
        operator_drill_passed=True,
        post_boot_workstream_passed=True,
        rollback_verified=True,
        protected_invariants_verified=True,
        paper_only_verified=True,
        unrelated_changes_preserved=True,
        pushed_remote=False,
        complete=True,
    )
    fields.update(overrides)
    return make_release_candidate_evidence(**fields)


def test_complete_release_candidate_is_ready_but_not_activated() -> None:
    first = evaluate_resilient_operations_release_candidate(_evidence())
    assert first == evaluate_resilient_operations_release_candidate(_evidence())
    assert first.status == "READY" and first.release_candidate_ready
    assert first.activation_requires_separate_authorization
    assert not any((first.restart_authorized, first.trading_authorized, first.execution_authorized))
    validate_release_candidate_result(first)


@pytest.mark.parametrize(
    "field",
    [
        "independent_verification_passed",
        "operator_drill_passed",
        "post_boot_workstream_passed",
        "rollback_verified",
        "protected_invariants_verified",
        "paper_only_verified",
        "unrelated_changes_preserved",
    ],
)
def test_each_required_safety_evidence_failure_denies(field) -> None:
    assert (
        evaluate_resilient_operations_release_candidate(_evidence(**{field: False})).status
        == "DENIED"
    )


def test_incomplete_coverage_low_tests_and_push_fail_closed() -> None:
    assert (
        evaluate_resilient_operations_release_candidate(_evidence(covered_phase_count=97)).status
        == "INCOMPLETE"
    )
    assert (
        evaluate_resilient_operations_release_candidate(
            _evidence(cumulative_tests_passed=693)
        ).status
        == "DENIED"
    )
    assert (
        evaluate_resilient_operations_release_candidate(_evidence(pushed_remote=True)).status
        == "DENIED"
    )


def test_evidence_result_tampering_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(ResilientOperationsReleaseCandidateError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_resilient_operations_release_candidate(replace(_evidence(), pushed_remote=True))
    result = evaluate_resilient_operations_release_candidate(_evidence())
    with pytest.raises(ResilientOperationsReleaseCandidateError, match="RESULT_HASH_MISMATCH"):
        validate_release_candidate_result(replace(result, cumulative_tests_passed=0))
    with pytest.raises(ResilientOperationsReleaseCandidateError, match="SAFETY_BOUNDARY"):
        validate_release_candidate_result(replace(result, trading_authorized=True))
    forbidden = {
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "push",
        "systemctl",
        "restart",
        "shutdown",
        "execute",
        "order",
    }
    assert forbidden.isdisjoint(evaluate_resilient_operations_release_candidate.__code__.co_names)
