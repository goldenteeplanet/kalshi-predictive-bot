from dataclasses import replace

import pytest

from kalshi_predictor.workstation.final_guarded_recovery_certification import (
    FinalGuardedRecoveryCertificationError,
    evaluate_final_guarded_recovery_certification,
    make_final_certification_evidence,
    validate_final_certification_result,
)


def _evidence(**overrides):
    fields = dict(
        release_candidate_hash="1" * 64,
        phase_ledger_hash="2" * 64,
        rollback_package_hash="3" * 64,
        covered_phase_count=99,
        cumulative_tests_passed=704,
        release_candidate_ready=True,
        all_phase_commits_verified=True,
        protected_invariants_verified=True,
        sole_writer_verified=True,
        wsl_running_observed=True,
        scheduler_active_observed=True,
        supervisor_activated=False,
        local_alerting_activated=False,
        restart_adapter_activated=False,
        rollback_verified=True,
        unrelated_changes_preserved=True,
        pushed_remote=False,
        complete=True,
    )
    fields.update(overrides)
    return make_final_certification_evidence(**fields)


def test_complete_evidence_certifies_guarded_not_activated_recovery() -> None:
    first = evaluate_final_guarded_recovery_certification(_evidence())
    assert first == evaluate_final_guarded_recovery_certification(_evidence())
    assert first.status == "CERTIFIED_GUARDED" and first.guarded_recovery_certified
    assert (
        first.activation_state == "NOT_ACTIVATED"
        and first.activation_requires_separate_authorization
    )
    assert not any((first.restart_authorized, first.trading_authorized, first.execution_authorized))
    validate_final_certification_result(first)


@pytest.mark.parametrize(
    "field",
    [
        "release_candidate_ready",
        "all_phase_commits_verified",
        "protected_invariants_verified",
        "sole_writer_verified",
        "wsl_running_observed",
        "scheduler_active_observed",
        "rollback_verified",
        "unrelated_changes_preserved",
    ],
)
def test_each_required_final_proof_denies(field) -> None:
    assert (
        evaluate_final_guarded_recovery_certification(_evidence(**{field: False})).status
        == "DENIED"
    )


@pytest.mark.parametrize(
    "field",
    [
        "supervisor_activated",
        "local_alerting_activated",
        "restart_adapter_activated",
        "pushed_remote",
    ],
)
def test_unapproved_activation_or_push_denies(field) -> None:
    assert (
        evaluate_final_guarded_recovery_certification(_evidence(**{field: True})).status == "DENIED"
    )


def test_incomplete_phase_and_test_coverage_fail_closed() -> None:
    assert (
        evaluate_final_guarded_recovery_certification(_evidence(covered_phase_count=98)).status
        == "INCOMPLETE"
    )
    assert (
        evaluate_final_guarded_recovery_certification(_evidence(cumulative_tests_passed=703)).status
        == "DENIED"
    )


def test_evidence_result_tampering_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(FinalGuardedRecoveryCertificationError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_final_guarded_recovery_certification(replace(_evidence(), pushed_remote=True))
    result = evaluate_final_guarded_recovery_certification(_evidence())
    with pytest.raises(FinalGuardedRecoveryCertificationError, match="RESULT_HASH_MISMATCH"):
        validate_final_certification_result(replace(result, cumulative_tests_passed=0))
    with pytest.raises(FinalGuardedRecoveryCertificationError, match="SAFETY_BOUNDARY"):
        validate_final_certification_result(replace(result, restart_authorized=True))
    forbidden = {
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "push",
        "schtasks",
        "systemctl",
        "restart",
        "shutdown",
        "execute",
        "order",
    }
    assert forbidden.isdisjoint(evaluate_final_guarded_recovery_certification.__code__.co_names)
