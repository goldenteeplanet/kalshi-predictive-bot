from dataclasses import replace

import pytest

from kalshi_predictor.workstation.failure_classification_workstream_gate import (
    REQUIRED_COMPONENTS,
    FailureClassificationWorkstreamGateError,
    evaluate_failure_classification_workstream_gate,
    make_failure_classification_component_evidence,
    validate_failure_classification_workstream_decision,
)


def _item(component, **overrides):
    fields = dict(
        component=component,
        artifact_hash=str(sorted(REQUIRED_COMPONENTS).index(component) + 1) * 64,
        verified=True,
        complete=True,
        safety_boundary_proven=True,
    )
    fields.update(overrides)
    return make_failure_classification_component_evidence(**fields)


def _all():
    return [_item(component) for component in REQUIRED_COMPONENTS]


def test_all_nine_components_produce_deterministic_non_authorizing_ready_gate() -> None:
    first = evaluate_failure_classification_workstream_gate(_all())
    second = evaluate_failure_classification_workstream_gate(list(reversed(_all())))
    assert first == second and first.status == "READY" and first.failure_classification_ready
    assert first.component_count == 9
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_failure_classification_workstream_decision(first)


def test_each_missing_component_is_incomplete() -> None:
    for missing in REQUIRED_COMPONENTS:
        result = evaluate_failure_classification_workstream_gate(
            [item for item in _all() if item.component != missing]
        )
        assert result.status == "INCOMPLETE"
        assert f"CLASSIFICATION_COMPONENT_MISSING:{missing}" in result.reasons


def test_incomplete_unverified_and_safety_unproven_components_block_readiness() -> None:
    for field in ("complete", "verified", "safety_boundary_proven"):
        records = _all()
        records[0] = _item(records[0].component, **{field: False})
        result = evaluate_failure_classification_workstream_gate(records)
        assert result.status in {"INCOMPLETE", "NOT_READY"}
        assert result.failure_classification_ready is False


def test_duplicate_unknown_excess_and_tampering_fail_closed() -> None:
    assert (
        evaluate_failure_classification_workstream_gate(
            [_item("CLOCK_SKEW_CLASSIFIER"), _item("CLOCK_SKEW_CLASSIFIER")]
        ).status
        == "TAMPERED"
    )
    unknown = make_failure_classification_component_evidence(
        component="UNKNOWN",
        artifact_hash="a" * 64,
        verified=True,
        complete=True,
        safety_boundary_proven=True,
    )
    assert evaluate_failure_classification_workstream_gate([unknown]).status == "TAMPERED"
    with pytest.raises(FailureClassificationWorkstreamGateError, match="BOUND_EXCEEDED"):
        evaluate_failure_classification_workstream_gate(_all(), max_records=8)
    with pytest.raises(FailureClassificationWorkstreamGateError, match="COMPONENT_HASH_MISMATCH"):
        evaluate_failure_classification_workstream_gate(
            [replace(_item("CLOCK_SKEW_CLASSIFIER"), verified=False)]
        )


def test_decision_safety_tampering_and_operational_surfaces_fail_closed() -> None:
    result = evaluate_failure_classification_workstream_gate(_all())
    with pytest.raises(FailureClassificationWorkstreamGateError, match="DECISION_HASH_MISMATCH"):
        validate_failure_classification_workstream_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(FailureClassificationWorkstreamGateError, match="SAFETY_BOUNDARY"):
        validate_failure_classification_workstream_decision(
            replace(result, host_restart_authorized=True)
        )
    forbidden = {
        "open",
        "connect",
        "subprocess",
        "socket",
        "restart",
        "reboot",
        "shutdown",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(evaluate_failure_classification_workstream_gate.__code__.co_names)
