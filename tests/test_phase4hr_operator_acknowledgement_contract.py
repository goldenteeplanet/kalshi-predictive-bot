from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.workstation.alert_rate_limit_storm_control import (
    evaluate_alert_rate_limit_and_storm_control,
)
from kalshi_predictor.workstation.alert_retry_backoff_policy import evaluate_alert_retry_backoff
from kalshi_predictor.workstation.alert_severity_deduplication import (
    evaluate_alert_severity_and_deduplication,
    make_alert_candidate,
)
from kalshi_predictor.workstation.operator_acknowledgement_contract import (
    OperatorAcknowledgementContractError,
    evaluate_operator_acknowledgement,
    make_operator_acknowledgement_record,
    validate_operator_acknowledgement_result,
)

INCIDENT_HASH = "c" * 64


def test_acknowledgement_is_verified_but_never_authorizes_control() -> None:
    admission = _admission()
    record = _record(admission)
    first = evaluate_operator_acknowledgement(
        admission,
        record,
        expected_incident_id_hash=INCIDENT_HASH,
        evaluated_at_epoch_seconds=200,
    )
    second = evaluate_operator_acknowledgement(
        admission,
        record,
        expected_incident_id_hash=INCIDENT_HASH,
        evaluated_at_epoch_seconds=200,
    )
    validate_operator_acknowledgement_result(first)
    assert first.status == "ACCEPTED"
    assert first.operator_acknowledged is True
    assert first.result_hash == second.result_hash
    assert first.recovery_authorized is False
    assert first.host_restart_authorized is False


def test_decline_and_diagnostics_are_distinct_non_authorizing_outcomes() -> None:
    admission = _admission()
    declined = evaluate_operator_acknowledgement(
        admission,
        _record(admission, action="DECLINED"),
        expected_incident_id_hash=INCIDENT_HASH,
        evaluated_at_epoch_seconds=200,
    )
    assert declined.status == "DECLINED"
    assert declined.operator_acknowledged is False
    diagnostics = evaluate_operator_acknowledgement(
        admission,
        _record(admission, action="REQUESTED_DIAGNOSTICS"),
        expected_incident_id_hash=INCIDENT_HASH,
        evaluated_at_epoch_seconds=200,
    )
    assert diagnostics.status == "DIAGNOSTICS_REQUESTED"
    assert diagnostics.diagnostics_requested is True
    assert diagnostics.service_control_authorized is False


def test_exact_expiry_and_ttl_boundaries_pass_then_expire() -> None:
    admission = _admission()
    exact = _record(admission, issued=100, acknowledged=200, expires=3_700)
    assert (
        evaluate_operator_acknowledgement(
            admission,
            exact,
            expected_incident_id_hash=INCIDENT_HASH,
            evaluated_at_epoch_seconds=3_700,
        ).status
        == "ACCEPTED"
    )
    assert (
        evaluate_operator_acknowledgement(
            admission,
            exact,
            expected_incident_id_hash=INCIDENT_HASH,
            evaluated_at_epoch_seconds=3_701,
        ).status
        == "STALE"
    )
    excessive = _record(admission, issued=100, acknowledged=200, expires=3_701)
    assert (
        evaluate_operator_acknowledgement(
            admission,
            excessive,
            expected_incident_id_hash=INCIDENT_HASH,
            evaluated_at_epoch_seconds=200,
        ).status
        == "DENIED"
    )


def test_incomplete_binding_before_issue_and_future_fail_closed() -> None:
    admission = _admission()
    incomplete = evaluate_operator_acknowledgement(
        admission,
        _record(admission, complete=False),
        expected_incident_id_hash=INCIDENT_HASH,
        evaluated_at_epoch_seconds=200,
    )
    assert incomplete.status == "INCOMPLETE"
    mismatched = evaluate_operator_acknowledgement(
        admission,
        _record(admission, incident="d" * 64),
        expected_incident_id_hash=INCIDENT_HASH,
        evaluated_at_epoch_seconds=200,
    )
    assert mismatched.status == "TAMPERED"
    before = evaluate_operator_acknowledgement(
        admission,
        _record(admission, issued=150, acknowledged=149),
        expected_incident_id_hash=INCIDENT_HASH,
        evaluated_at_epoch_seconds=200,
    )
    assert before.status == "DENIED"
    future = evaluate_operator_acknowledgement(
        admission,
        _record(admission, acknowledged=201),
        expected_incident_id_hash=INCIDENT_HASH,
        evaluated_at_epoch_seconds=200,
    )
    assert future.status == "DENIED"


def test_malformed_bounds_record_and_admission_tampering_fail_closed() -> None:
    admission = _admission()
    with pytest.raises(OperatorAcknowledgementContractError, match="ACKNOWLEDGEMENT_FIELD_INVALID"):
        _record(admission, action="AUTHORIZE_RESTART")
    with pytest.raises(OperatorAcknowledgementContractError, match="CONTRACT_BOUND_INVALID"):
        evaluate_operator_acknowledgement(
            admission,
            _record(admission),
            expected_incident_id_hash=INCIDENT_HASH,
            evaluated_at_epoch_seconds=True,
        )
    record = _record(admission)
    with pytest.raises(OperatorAcknowledgementContractError, match="ACKNOWLEDGEMENT_HASH_MISMATCH"):
        evaluate_operator_acknowledgement(
            admission,
            replace(record, action="DECLINED"),
            expected_incident_id_hash=INCIDENT_HASH,
            evaluated_at_epoch_seconds=200,
        )
    with pytest.raises(OperatorAcknowledgementContractError, match="ALERT_ADMISSION_INVALID"):
        evaluate_operator_acknowledgement(
            replace(admission, decision_hash="0" * 64),
            record,
            expected_incident_id_hash=INCIDENT_HASH,
            evaluated_at_epoch_seconds=200,
        )


def test_result_and_safety_tampering_fail_closed() -> None:
    admission = _admission()
    result = evaluate_operator_acknowledgement(
        admission,
        _record(admission),
        expected_incident_id_hash=INCIDENT_HASH,
        evaluated_at_epoch_seconds=200,
    )
    with pytest.raises(OperatorAcknowledgementContractError, match="RESULT_HASH_MISMATCH"):
        validate_operator_acknowledgement_result(replace(result, result_hash="0" * 64))
    with pytest.raises(
        OperatorAcknowledgementContractError, match="RESULT_SAFETY_BOUNDARY_INVALID"
    ):
        validate_operator_acknowledgement_result(replace(result, recovery_authorized=True))


def test_contract_has_no_delivery_file_service_or_restart_surface() -> None:
    names = set(evaluate_operator_acknowledgement.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "commit",
            "connect",
            "execute",
            "open",
            "restart",
            "shutdown",
            "show",
            "start",
            "stop",
            "systemctl",
            "toast",
            "write",
        }
    )


def _admission():
    candidate = make_alert_candidate(
        incident_id="incident-1",
        category="WSL_LIVENESS",
        reason_code="WSL_UNAVAILABLE",
        observed_at_epoch_seconds=100,
        evidence_age_seconds=1,
        evidence_hash="a" * 64,
        complete=True,
        source_identity_hash="b" * 64,
    )
    alert = evaluate_alert_severity_and_deduplication(candidate, [], evaluated_at_epoch_seconds=100)
    retry = evaluate_alert_retry_backoff(alert, [], evaluated_at_epoch_seconds=100)
    return evaluate_alert_rate_limit_and_storm_control(
        retry, "WARNING", [], evaluated_at_epoch_seconds=100
    )


def _record(
    admission,
    *,
    incident=INCIDENT_HASH,
    issued=100,
    acknowledged=150,
    expires=300,
    action="ACKNOWLEDGED",
    complete=True,
):
    return make_operator_acknowledgement_record(
        acknowledgement_id="ack-1",
        incident_id_hash=incident,
        alert_admission_hash=admission.decision_hash,
        issued_at_epoch_seconds=issued,
        acknowledged_at_epoch_seconds=acknowledged,
        expires_at_epoch_seconds=expires,
        action=action,
        operator_identity_hash="d" * 64,
        complete=complete,
    )
