from dataclasses import replace

import pytest

from kalshi_predictor.workstation.component_recovery_workstream_gate import (
    REQUIRED_COMPONENTS,
    ComponentRecoveryWorkstreamGateError,
    evaluate_component_recovery_workstream_gate,
    make_component_recovery_evidence,
    validate_component_recovery_workstream_decision,
)


def _item(component, **overrides):
    fields = dict(
        component=component,
        artifact_hash=f"{sorted(REQUIRED_COMPONENTS).index(component) + 1:064x}",
        verified=True,
        complete=True,
        safety_proven=True,
        dry_run_only=True,
    )
    fields.update(overrides)
    return make_component_recovery_evidence(**fields)


def _all():
    return [_item(component) for component in REQUIRED_COMPONENTS]


def test_all_nine_components_produce_deterministic_non_authorizing_ready_gate() -> None:
    first = evaluate_component_recovery_workstream_gate(_all())
    second = evaluate_component_recovery_workstream_gate(list(reversed(_all())))
    assert first == second and first.status == "READY"
    assert first.component_recovery_workstream_ready and first.component_count == 9
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.wsl_shutdown_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_component_recovery_workstream_decision(first)


def test_each_missing_component_is_incomplete() -> None:
    for missing in REQUIRED_COMPONENTS:
        result = evaluate_component_recovery_workstream_gate(
            [item for item in _all() if item.component != missing]
        )
        assert result.status == "INCOMPLETE"
        assert f"COMPONENT_RECOVERY_COMPONENT_MISSING:{missing}" in result.reasons


def test_incomplete_unverified_unsafe_or_non_dry_run_components_block() -> None:
    for field in ("complete", "verified", "safety_proven", "dry_run_only"):
        records = _all()
        records[0] = _item(records[0].component, **{field: False})
        result = evaluate_component_recovery_workstream_gate(records)
        assert result.status in {"INCOMPLETE", "NOT_READY"}
        assert result.component_recovery_workstream_ready is False


def test_duplicate_unknown_excess_and_evidence_tampering_fail_closed() -> None:
    duplicate = [_item("WSL_WAKE_DRY_RUN_PLANNER")] * 2
    assert evaluate_component_recovery_workstream_gate(duplicate).status == "TAMPERED"
    unknown = make_component_recovery_evidence(
        component="UNKNOWN",
        artifact_hash="a" * 64,
        verified=True,
        complete=True,
        safety_proven=True,
        dry_run_only=True,
    )
    assert evaluate_component_recovery_workstream_gate([unknown]).status == "TAMPERED"
    with pytest.raises(ComponentRecoveryWorkstreamGateError, match="BOUND_EXCEEDED"):
        evaluate_component_recovery_workstream_gate(_all(), max_records=8)
    with pytest.raises(ComponentRecoveryWorkstreamGateError, match="HASH_MISMATCH"):
        evaluate_component_recovery_workstream_gate(
            [replace(_item("WSL_WAKE_DRY_RUN_PLANNER"), verified=False)]
        )


def test_decision_tampering_safety_and_operational_surfaces_fail_closed() -> None:
    result = evaluate_component_recovery_workstream_gate(_all())
    with pytest.raises(ComponentRecoveryWorkstreamGateError, match="HASH_MISMATCH"):
        validate_component_recovery_workstream_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(ComponentRecoveryWorkstreamGateError, match="SAFETY_BOUNDARY"):
        validate_component_recovery_workstream_decision(
            replace(result, host_restart_authorized=True)
        )
    forbidden = {"open", "run", "popen", "subprocess", "socket", "restart", "execute"}
    assert forbidden.isdisjoint(evaluate_component_recovery_workstream_gate.__code__.co_names)
