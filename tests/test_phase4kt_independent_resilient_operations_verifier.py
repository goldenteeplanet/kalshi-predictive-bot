from dataclasses import replace

import pytest

from kalshi_predictor.workstation.independent_resilient_operations_verifier import (
    REQUIRED_PHASES,
    IndependentResilientOperationsVerifierError,
    evaluate_independent_resilient_operations_verifier,
    make_phase_safety_evidence,
    validate_independent_verification_result,
)


def _records(**override):
    result = []
    for index, phase in enumerate(REQUIRED_PHASES):
        fields = dict(
            phase=phase,
            artifact_hash=f"{index + 1:064x}",
            tests_passed=1,
            safety_verified=True,
            independent_review=True,
            complete=True,
        )
        if phase == override.get("phase"):
            fields.update({key: value for key, value in override.items() if key != "phase"})
        result.append(make_phase_safety_evidence(**fields))
    return tuple(result)


def test_exact_96_phase_independent_evidence_certifies_paper_only() -> None:
    assert (
        len(REQUIRED_PHASES) == 96 and REQUIRED_PHASES[0] == "4HB" and REQUIRED_PHASES[-1] == "4KS"
    )
    first = evaluate_independent_resilient_operations_verifier(_records())
    assert first == evaluate_independent_resilient_operations_verifier(_records())
    assert (
        first.status == "CERTIFIED"
        and first.independent_verification_proven
        and first.paper_only_proven
    )
    assert not any(
        (
            first.recovery_authorized,
            first.restart_authorized,
            first.trading_authorized,
            first.execution_authorized,
        )
    )
    validate_independent_verification_result(first)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tests_passed", 0),
        ("safety_verified", False),
        ("independent_review", False),
        ("complete", False),
    ],
)
def test_missing_test_safety_review_or_completeness_denies(field, value) -> None:
    result = evaluate_independent_resilient_operations_verifier(
        _records(phase="4KS", **{field: value})
    )
    assert result.status == "DENIED" and not result.independent_verification_proven


def test_missing_reordered_or_duplicate_phase_evidence_fails_closed() -> None:
    records = _records()
    assert evaluate_independent_resilient_operations_verifier(records[:-1]).status == "INCOMPLETE"
    assert (
        evaluate_independent_resilient_operations_verifier(tuple(reversed(records))).status
        == "INCOMPLETE"
    )
    assert (
        evaluate_independent_resilient_operations_verifier(records[:-1] + (records[0],)).status
        == "TAMPERED"
    )


def test_field_result_tampering_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(IndependentResilientOperationsVerifierError, match="EVIDENCE_FIELD_INVALID"):
        make_phase_safety_evidence(
            phase="4KT",
            artifact_hash="bad",
            tests_passed=1,
            safety_verified=True,
            independent_review=True,
            complete=True,
        )
    result = evaluate_independent_resilient_operations_verifier(_records())
    with pytest.raises(IndependentResilientOperationsVerifierError, match="RESULT_HASH_MISMATCH"):
        validate_independent_verification_result(replace(result, total_tests_passed=0))
    with pytest.raises(IndependentResilientOperationsVerifierError, match="SAFETY_BOUNDARY"):
        validate_independent_verification_result(replace(result, trading_authorized=True))
    forbidden = {
        "open",
        "write",
        "run",
        "Popen",
        "subprocess",
        "systemctl",
        "restart",
        "shutdown",
        "execute",
        "order",
    }
    assert forbidden.isdisjoint(
        evaluate_independent_resilient_operations_verifier.__code__.co_names
    )
