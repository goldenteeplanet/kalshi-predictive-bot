from dataclasses import replace

import pytest

from kalshi_predictor.workstation.test_host_restart_prohibition import (
    HostTestRestartProhibitionError,
    evaluate_test_host_restart_prohibition,
    make_test_host_evidence,
    validate_test_host_prohibition_decision,
)


def _evidence(**overrides):
    fields = dict(
        host_identity_hash="1" * 64,
        process_evidence_hash="2" * 64,
        environment_evidence_hash="3" * 64,
        lock_evidence_hash="4" * 64,
        test_process_detected=False,
        test_environment_detected=False,
        test_lock_present=False,
        evidence_complete=True,
        integrity_verified=True,
    )
    fields.update(overrides)
    return make_test_host_evidence(**fields)


def test_verified_non_test_host_is_clear_but_not_authorized() -> None:
    first = evaluate_test_host_restart_prohibition(_evidence())
    assert first == evaluate_test_host_restart_prohibition(_evidence())
    assert first.status == "CLEAR" and first.test_host_clear
    assert not first.restart_prohibited and not first.restart_authorized
    validate_test_host_prohibition_decision(first)


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("test_process_detected", "TEST_PROCESS_DETECTED"),
        ("test_environment_detected", "TEST_ENVIRONMENT_DETECTED"),
        ("test_lock_present", "TEST_LOCK_PRESENT"),
    ],
)
def test_each_test_marker_absolutely_prohibits_restart(field, reason) -> None:
    result = evaluate_test_host_restart_prohibition(_evidence(**{field: True}))
    assert result.status == "PROHIBITED" and result.restart_prohibited
    assert reason in result.reasons and not result.restart_authorized


def test_multiple_test_markers_are_all_preserved_deterministically() -> None:
    result = evaluate_test_host_restart_prohibition(
        _evidence(
            test_process_detected=True, test_environment_detected=True, test_lock_present=True
        )
    )
    assert result.marker_count == 3
    assert result.reasons == (
        "TEST_PROCESS_DETECTED",
        "TEST_ENVIRONMENT_DETECTED",
        "TEST_LOCK_PRESENT",
    )


@pytest.mark.parametrize("overrides", [{"evidence_complete": False}, {"integrity_verified": False}])
def test_incomplete_or_unverified_evidence_denies(overrides) -> None:
    result = evaluate_test_host_restart_prohibition(_evidence(**overrides))
    assert result.status == "DENIED" and result.restart_prohibited


def test_malformed_evidence_and_tampering_fail_closed() -> None:
    with pytest.raises(HostTestRestartProhibitionError, match="FIELD_INVALID"):
        _evidence(host_identity_hash="bad")
    evidence = _evidence()
    with pytest.raises(HostTestRestartProhibitionError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_test_host_restart_prohibition(replace(evidence, test_lock_present=True))
    decision = evaluate_test_host_restart_prohibition(evidence)
    with pytest.raises(HostTestRestartProhibitionError, match="DECISION_HASH_MISMATCH"):
        validate_test_host_prohibition_decision(replace(decision, marker_count=1))
    with pytest.raises(HostTestRestartProhibitionError, match="SAFETY_BOUNDARY"):
        validate_test_host_prohibition_decision(replace(decision, restart_authorized=True))


def test_prohibition_evaluator_has_no_operational_surfaces() -> None:
    forbidden = {"open", "run", "Popen", "subprocess", "system", "spawn", "socket", "shutdown"}
    assert forbidden.isdisjoint(evaluate_test_host_restart_prohibition.__code__.co_names)
