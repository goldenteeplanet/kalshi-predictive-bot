from dataclasses import replace

import pytest

from kalshi_predictor.workstation.alerting_workstream_gate import (
    REQUIRED_COMPONENTS,
    AlertingWorkstreamGateError,
    evaluate_alerting_workstream_gate,
    make_alerting_component_evidence,
    validate_alerting_workstream_decision,
)


def _evidence(component, *, verified=True, complete=True):
    return make_alerting_component_evidence(
        component=component,
        artifact_hash=(str(sorted(REQUIRED_COMPONENTS).index(component) + 1) * 64),
        verified=verified,
        complete=complete,
    )


def _all():
    return [_evidence(component) for component in REQUIRED_COMPONENTS]


def test_all_six_verified_components_produce_deterministic_ready_gate() -> None:
    first = evaluate_alerting_workstream_gate(_all(), evaluated_at_epoch_seconds=100)
    second = evaluate_alerting_workstream_gate(
        reversed_list := list(reversed(_all())), evaluated_at_epoch_seconds=100
    )
    assert reversed_list
    assert first == second
    assert first.status == "READY" and first.alerting_workstream_ready
    assert first.component_count == 6
    assert not any(
        (
            first.alert_delivery_authorized,
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_alerting_workstream_decision(first)


def test_missing_and_incomplete_evidence_fail_closed() -> None:
    missing = evaluate_alerting_workstream_gate(_all()[:-1], evaluated_at_epoch_seconds=100)
    records = _all()
    records[0] = _evidence(records[0].component, complete=False)
    incomplete = evaluate_alerting_workstream_gate(records, evaluated_at_epoch_seconds=100)
    assert missing.status == "INCOMPLETE"
    assert incomplete.status == "INCOMPLETE"


def test_unverified_evidence_is_not_ready() -> None:
    records = _all()
    records[0] = _evidence(records[0].component, verified=False)
    result = evaluate_alerting_workstream_gate(records, evaluated_at_epoch_seconds=100)
    assert result.status == "NOT_READY"
    assert result.alerting_workstream_ready is False


def test_duplicates_are_tampered_and_bounds_are_enforced() -> None:
    duplicate = [_evidence("RETRY_BACKOFF"), _evidence("RETRY_BACKOFF")]
    assert (
        evaluate_alerting_workstream_gate(duplicate, evaluated_at_epoch_seconds=100).status
        == "TAMPERED"
    )
    with pytest.raises(AlertingWorkstreamGateError, match="BOUND_EXCEEDED"):
        evaluate_alerting_workstream_gate(_all(), evaluated_at_epoch_seconds=100, max_records=5)


def test_malformed_and_evidence_tampering_fail_closed() -> None:
    with pytest.raises(AlertingWorkstreamGateError, match="FIELD_INVALID"):
        make_alerting_component_evidence(
            component="UNKNOWN", artifact_hash="a" * 64, verified=True, complete=True
        )
    records = _all()
    records[0] = replace(records[0], verified=False)
    with pytest.raises(AlertingWorkstreamGateError, match="HASH_MISMATCH"):
        evaluate_alerting_workstream_gate(records, evaluated_at_epoch_seconds=100)


def test_decision_and_safety_tampering_fail_closed() -> None:
    result = evaluate_alerting_workstream_gate(_all(), evaluated_at_epoch_seconds=100)
    with pytest.raises(AlertingWorkstreamGateError, match="DECISION_HASH_MISMATCH"):
        validate_alerting_workstream_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(AlertingWorkstreamGateError, match="SAFETY_BOUNDARY"):
        validate_alerting_workstream_decision(replace(result, execution_authorized=True))


def test_gate_has_no_io_delivery_recovery_restart_or_execution_surface() -> None:
    forbidden = {
        "open",
        "subprocess",
        "socket",
        "send",
        "notify",
        "restart",
        "reboot",
        "shutdown",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(evaluate_alerting_workstream_gate.__code__.co_names)
