from dataclasses import replace

import pytest

from kalshi_predictor.workstation.diagnostics_workstream_gate import (
    REQUIRED_COMPONENTS,
    DiagnosticsWorkstreamGateError,
    evaluate_diagnostics_workstream_gate,
    make_diagnostics_component_evidence,
    validate_diagnostics_workstream_decision,
)


def _item(component, **overrides):
    fields = dict(
        component=component,
        artifact_hash=str(sorted(REQUIRED_COMPONENTS).index(component) + 1) * 64,
        verified=True,
        complete=True,
        bounds_proven=True,
        redaction_proven=True,
    )
    fields.update(overrides)
    return make_diagnostics_component_evidence(**fields)


def _all():
    return [_item(component) for component in REQUIRED_COMPONENTS]


def test_all_nine_components_produce_deterministic_non_authorizing_ready_gate() -> None:
    first = evaluate_diagnostics_workstream_gate(_all())
    second = evaluate_diagnostics_workstream_gate(list(reversed(_all())))
    assert first == second and first.status == "READY" and first.diagnostics_workstream_ready
    assert first.component_count == 9
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_diagnostics_workstream_decision(first)


def test_each_missing_component_is_incomplete() -> None:
    for missing in REQUIRED_COMPONENTS:
        result = evaluate_diagnostics_workstream_gate(
            [item for item in _all() if item.component != missing]
        )
        assert result.status == "INCOMPLETE"
        assert f"DIAGNOSTICS_COMPONENT_MISSING:{missing}" in result.reasons


def test_incomplete_unverified_unbounded_and_unredacted_components_block() -> None:
    for field in ("complete", "verified", "bounds_proven", "redaction_proven"):
        records = _all()
        records[0] = _item(records[0].component, **{field: False})
        result = evaluate_diagnostics_workstream_gate(records)
        assert result.status in {"INCOMPLETE", "NOT_READY"}
        assert result.diagnostics_workstream_ready is False


def test_duplicate_unknown_excess_and_tampering_fail_closed() -> None:
    assert (
        evaluate_diagnostics_workstream_gate(
            [_item("WSL_STATUS_CAPTURE"), _item("WSL_STATUS_CAPTURE")]
        ).status
        == "TAMPERED"
    )
    unknown = make_diagnostics_component_evidence(
        component="UNKNOWN",
        artifact_hash="a" * 64,
        verified=True,
        complete=True,
        bounds_proven=True,
        redaction_proven=True,
    )
    assert evaluate_diagnostics_workstream_gate([unknown]).status == "TAMPERED"
    with pytest.raises(DiagnosticsWorkstreamGateError, match="BOUND_EXCEEDED"):
        evaluate_diagnostics_workstream_gate(_all(), max_records=8)
    with pytest.raises(DiagnosticsWorkstreamGateError, match="COMPONENT_HASH_MISMATCH"):
        evaluate_diagnostics_workstream_gate([replace(_item("WSL_STATUS_CAPTURE"), verified=False)])


def test_decision_safety_tampering_and_operational_surfaces_fail_closed() -> None:
    result = evaluate_diagnostics_workstream_gate(_all())
    with pytest.raises(DiagnosticsWorkstreamGateError, match="DECISION_HASH_MISMATCH"):
        validate_diagnostics_workstream_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(DiagnosticsWorkstreamGateError, match="SAFETY_BOUNDARY"):
        validate_diagnostics_workstream_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "open",
        "run",
        "popen",
        "subprocess",
        "socket",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(evaluate_diagnostics_workstream_gate.__code__.co_names)
