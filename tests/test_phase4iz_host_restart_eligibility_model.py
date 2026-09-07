from dataclasses import replace

import pytest
from kalshi_predictor.workstation.host_restart_eligibility_model import (
    HostRestartEligibilityModelError,
    evaluate_host_restart_eligibility,
    make_host_restart_eligibility_evidence,
    validate_host_restart_eligibility_decision,
)


def _evidence(**overrides):
    fields = dict(
        incident_id_hash="1" * 64,
        classifier_decision_hash="2" * 64,
        recovery_decision_hash="3" * 64,
        diagnostics_hash="4" * 64,
        invariant_evidence_hash="5" * 64,
        writer_evidence_hash="6" * 64,
        classifier_status="HOST_RESTART_REQUIRED",
        component_recovery_failed=True,
        evidence_complete=True,
        trading_fail_closed=True,
        invariants_unchanged=True,
        writer_exclusive=True,
        test_host=False,
    )
    fields.update(overrides)
    return make_host_restart_eligibility_evidence(**fields)


def test_exact_policy_evidence_is_deterministically_eligible_but_never_authorized() -> None:
    first = evaluate_host_restart_eligibility(_evidence())
    second = evaluate_host_restart_eligibility(_evidence())
    assert first == second and first.status == "ELIGIBLE" and first.eligibility_proven
    assert first.warning_required and first.cancellation_required
    assert first.cooldown_check_required
    assert not any(
        (first.restart_authorized, first.service_control_authorized, first.execution_authorized)
    )
    validate_host_restart_eligibility_decision(first)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"classifier_status": "RESTART_DENIED"}, "CLASSIFIER_DENIED"),
        ({"component_recovery_failed": False}, "RECOVERY_NOT_FAILED"),
        ({"trading_fail_closed": False}, "TRADING_NOT_FAIL_CLOSED"),
        ({"invariants_unchanged": False}, "INVARIANT_CHANGED"),
        ({"writer_exclusive": False}, "WRITER_EXCLUSIVITY_UNPROVEN"),
        ({"test_host": True}, "TEST_HOST_DENIED"),
    ],
)
def test_each_restart_precondition_fails_closed(overrides, reason) -> None:
    result = evaluate_host_restart_eligibility(_evidence(**overrides))
    assert result.status == "DENIED" and not result.eligibility_proven
    assert any(reason in item for item in result.reasons)


def test_partial_and_malformed_evidence_fail_closed() -> None:
    result = evaluate_host_restart_eligibility(_evidence(evidence_complete=False))
    assert result.status == "INCOMPLETE" and not result.eligibility_proven
    with pytest.raises(HostRestartEligibilityModelError, match="FIELD_INVALID"):
        make_host_restart_eligibility_evidence(
            **{
                **_evidence().__dict__,
                "incident_id_hash": "bad",
                "evidence_hash": "ignored",
            }
        )


def test_evidence_decision_and_safety_tampering_fail_closed() -> None:
    evidence = _evidence()
    with pytest.raises(HostRestartEligibilityModelError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_host_restart_eligibility(replace(evidence, test_host=True))
    decision = evaluate_host_restart_eligibility(evidence)
    with pytest.raises(HostRestartEligibilityModelError, match="DECISION_HASH_MISMATCH"):
        validate_host_restart_eligibility_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(HostRestartEligibilityModelError, match="SAFETY_BOUNDARY"):
        validate_host_restart_eligibility_decision(replace(decision, restart_authorized=True))


def test_model_has_no_operational_surfaces() -> None:
    forbidden = {
        "open",
        "run",
        "popen",
        "subprocess",
        "socket",
        "shutdown",
        "restart_computer",
        "systemctl",
        "order",
    }
    assert forbidden.isdisjoint(evaluate_host_restart_eligibility.__code__.co_names)
