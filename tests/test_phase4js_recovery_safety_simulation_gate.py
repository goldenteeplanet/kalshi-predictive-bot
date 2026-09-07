from dataclasses import replace

import pytest

from kalshi_predictor.workstation.recovery_safety_simulation_gate import (
    REQUIRED_COMPONENTS,
    RecoverySafetySimulationGateError,
    evaluate_recovery_safety_simulation_gate,
    make_recovery_safety_simulation_evidence,
    validate_recovery_safety_simulation_decision,
)


def _item(component, **overrides):
    fields = dict(
        component=component,
        artifact_hash=f"{sorted(REQUIRED_COMPONENTS).index(component) + 1:064x}",
        verified=True,
        complete=True,
        safety_proven=True,
        fixture_or_read_only=True,
    )
    fields.update(overrides)
    return make_recovery_safety_simulation_evidence(**fields)


def _all():
    return [_item(component) for component in REQUIRED_COMPONENTS]


def test_all_nine_components_make_deterministic_ready_non_authorizing_gate() -> None:
    first = evaluate_recovery_safety_simulation_gate(_all())
    second = evaluate_recovery_safety_simulation_gate(list(reversed(_all())))
    assert first == second and first.status == "READY"
    assert first.recovery_safety_simulation_ready and first.component_count == 9
    assert first.deployment_review_required and first.operator_activation_required
    assert not any(
        (
            first.restart_authorized,
            first.process_spawn_authorized,
            first.service_control_authorized,
            first.execution_authorized,
        )
    )
    validate_recovery_safety_simulation_decision(first)


def test_each_missing_component_is_incomplete() -> None:
    for missing in REQUIRED_COMPONENTS:
        result = evaluate_recovery_safety_simulation_gate(
            [item for item in _all() if item.component != missing]
        )
        assert result.status == "INCOMPLETE"
        assert f"RECOVERY_SIMULATION_COMPONENT_MISSING:{missing}" in result.reasons


def test_incomplete_unverified_unsafe_or_non_fixture_components_block() -> None:
    for field in ("complete", "verified", "safety_proven", "fixture_or_read_only"):
        records = _all()
        records[0] = _item(records[0].component, **{field: False})
        result = evaluate_recovery_safety_simulation_gate(records)
        assert result.status in {"INCOMPLETE", "NOT_READY"}
        assert not result.recovery_safety_simulation_ready


def test_duplicate_unknown_excess_and_evidence_tampering_fail_closed() -> None:
    duplicate = [_item("MOCK_RESTART_EXECUTOR")] * 2
    assert evaluate_recovery_safety_simulation_gate(duplicate).status == "TAMPERED"
    unknown = make_recovery_safety_simulation_evidence(
        component="UNKNOWN",
        artifact_hash="a" * 64,
        verified=True,
        complete=True,
        safety_proven=True,
        fixture_or_read_only=True,
    )
    assert evaluate_recovery_safety_simulation_gate([unknown]).status == "TAMPERED"
    with pytest.raises(RecoverySafetySimulationGateError, match="BOUND_EXCEEDED"):
        evaluate_recovery_safety_simulation_gate(_all(), max_records=8)
    with pytest.raises(RecoverySafetySimulationGateError, match="HASH_MISMATCH"):
        evaluate_recovery_safety_simulation_gate(
            [replace(_item("MOCK_RESTART_EXECUTOR"), verified=False)]
        )


def test_decision_authority_tampering_and_operational_surfaces_fail_closed() -> None:
    decision = evaluate_recovery_safety_simulation_gate(_all())
    with pytest.raises(RecoverySafetySimulationGateError, match="HASH_MISMATCH"):
        validate_recovery_safety_simulation_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(RecoverySafetySimulationGateError, match="SAFETY_BOUNDARY"):
        validate_recovery_safety_simulation_decision(replace(decision, restart_authorized=True))
    forbidden = {"open", "write", "run", "Popen", "subprocess", "system", "spawn", "socket"}
    assert forbidden.isdisjoint(evaluate_recovery_safety_simulation_gate.__code__.co_names)
