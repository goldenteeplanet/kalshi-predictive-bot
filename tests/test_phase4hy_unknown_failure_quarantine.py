from dataclasses import replace

import pytest

from kalshi_predictor.workstation.critical_dependency_allowlist import (
    evaluate_critical_dependency,
    make_dependency_observation,
)
from kalshi_predictor.workstation.failure_observation_quorum import (
    evaluate_failure_observation_quorum,
    make_failure_observation,
)
from kalshi_predictor.workstation.failure_persistence_window import (
    evaluate_failure_persistence_window,
)
from kalshi_predictor.workstation.unknown_failure_quarantine import (
    UnknownFailureQuarantineError,
    evaluate_unknown_failure_quarantine,
    make_failure_classification_claim,
    validate_unknown_failure_quarantine_decision,
)


def _persistence(persistent=True):
    dep = evaluate_critical_dependency(
        make_dependency_observation(
            observation_id_hash="a" * 64,
            dependency_code="WSL_VM",
            observed_at_epoch_seconds=1,
            complete=True,
        ),
        evaluated_at_epoch_seconds=1,
    )

    def quorum(at, digit):
        records = [
            make_failure_observation(
                observation_id_hash=str(digit) * 64,
                source_code="A",
                dependency_code="WSL_VM",
                observed_at_epoch_seconds=at,
                failure_observed=True,
                complete=True,
            ),
            make_failure_observation(
                observation_id_hash=str(digit + 1) * 64,
                source_code="B",
                dependency_code="WSL_VM",
                observed_at_epoch_seconds=at,
                failure_observed=True,
                complete=True,
            ),
        ]
        return evaluate_failure_observation_quorum(dep, records, evaluated_at_epoch_seconds=at)

    history = [quorum(100, 1), quorum(160 if persistent else 120, 3)]
    return evaluate_failure_persistence_window(
        history, dependency_code="WSL_VM", evaluated_at_epoch_seconds=160 if persistent else 120
    )


def _claim(persistence, failure_class="UNEXPLAINED_FAILURE", **overrides):
    fields = dict(
        incident_id_hash="f" * 64,
        persistence_decision_hash=persistence.decision_hash,
        failure_class=failure_class,
        classified_at_epoch_seconds=160,
        complete=True,
    )
    fields.update(overrides)
    return make_failure_classification_claim(**fields)


def test_unknown_persistent_failure_is_quarantined_and_alerted_without_authority() -> None:
    persistence = _persistence()
    result = evaluate_unknown_failure_quarantine(
        persistence, _claim(persistence), evaluated_at_epoch_seconds=160
    )
    assert result.status == "QUARANTINED" and result.quarantined and result.operator_alert_required
    assert not any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    )
    validate_unknown_failure_quarantine_decision(result)


@pytest.mark.parametrize(
    "failure_class", ["WSL_VM_UNAVAILABLE", "SYSTEMD_USER_UNAVAILABLE", "SCHEDULER_UNAVAILABLE"]
)
def test_known_classes_proceed_only_as_classified_evidence(failure_class) -> None:
    persistence = _persistence()
    result = evaluate_unknown_failure_quarantine(
        persistence, _claim(persistence, failure_class), evaluated_at_epoch_seconds=160
    )
    assert result.status == "CLASSIFIED" and result.failure_classified
    assert result.recovery_authorized is False


def test_nonpersistent_incomplete_future_and_binding_mismatch_fail_closed() -> None:
    transient = _persistence(False)
    assert (
        evaluate_unknown_failure_quarantine(
            transient,
            _claim(transient, classified_at_epoch_seconds=120),
            evaluated_at_epoch_seconds=120,
        ).status
        == "NOT_PERSISTENT"
    )
    persistent = _persistence()
    assert (
        evaluate_unknown_failure_quarantine(
            persistent, _claim(persistent, complete=False), evaluated_at_epoch_seconds=160
        ).status
        == "INCOMPLETE"
    )
    assert (
        evaluate_unknown_failure_quarantine(
            persistent,
            _claim(persistent, classified_at_epoch_seconds=161),
            evaluated_at_epoch_seconds=160,
        ).status
        == "TAMPERED"
    )
    other = replace(_claim(persistent), persistence_decision_hash="0" * 64)
    with pytest.raises(UnknownFailureQuarantineError, match="HASH_MISMATCH"):
        evaluate_unknown_failure_quarantine(persistent, other, evaluated_at_epoch_seconds=160)


def test_claim_result_safety_tampering_and_operational_surfaces_fail_closed() -> None:
    persistence = _persistence()
    claim = _claim(persistence)
    with pytest.raises(UnknownFailureQuarantineError, match="CLAIM_HASH_MISMATCH"):
        evaluate_unknown_failure_quarantine(
            persistence, replace(claim, complete=False), evaluated_at_epoch_seconds=160
        )
    result = evaluate_unknown_failure_quarantine(persistence, claim, evaluated_at_epoch_seconds=160)
    with pytest.raises(UnknownFailureQuarantineError, match="RESULT_HASH_MISMATCH"):
        validate_unknown_failure_quarantine_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(UnknownFailureQuarantineError, match="SAFETY_BOUNDARY"):
        validate_unknown_failure_quarantine_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "open",
        "subprocess",
        "socket",
        "restart",
        "reboot",
        "shutdown",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(evaluate_unknown_failure_quarantine.__code__.co_names)
