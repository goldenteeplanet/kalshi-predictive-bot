from dataclasses import replace

import pytest

from kalshi_predictor.workstation.supervisor_deployment_gate import (
    REQUIRED_COMPONENTS,
    SupervisorDeploymentGateError,
    evaluate_supervisor_deployment_gate,
    make_supervisor_deployment_evidence,
    validate_supervisor_deployment_decision,
)


def _item(component, **overrides):
    fields = dict(
        component=component,
        artifact_hash=f"{sorted(REQUIRED_COMPONENTS).index(component) + 1:064x}",
        verified=True,
        complete=True,
        safety_proven=True,
        activation_disabled=True,
    )
    fields.update(overrides)
    return make_supervisor_deployment_evidence(**fields)


def _all():
    return [_item(component) for component in REQUIRED_COMPONENTS]


def test_all_nine_components_make_deterministic_ready_but_non_activating_gate() -> None:
    first = evaluate_supervisor_deployment_gate(_all())
    second = evaluate_supervisor_deployment_gate(list(reversed(_all())))
    assert first == second and first.status == "READY"
    assert first.deployment_evidence_ready and first.component_count == 9
    assert first.explicit_operator_activation_required
    assert not any(
        (
            first.task_activation_authorized,
            first.configuration_use_authorized,
            first.restart_authorized,
            first.process_spawn_authorized,
            first.execution_authorized,
        )
    )
    validate_supervisor_deployment_decision(first)


def test_each_missing_component_is_incomplete() -> None:
    for missing in REQUIRED_COMPONENTS:
        result = evaluate_supervisor_deployment_gate(
            [item for item in _all() if item.component != missing]
        )
        assert result.status == "INCOMPLETE"
        assert f"SUPERVISOR_DEPLOYMENT_COMPONENT_MISSING:{missing}" in result.reasons


def test_incomplete_unverified_unsafe_or_activated_components_block() -> None:
    for field in ("complete", "verified", "safety_proven", "activation_disabled"):
        records = _all()
        records[0] = _item(records[0].component, **{field: False})
        result = evaluate_supervisor_deployment_gate(records)
        assert result.status in {"INCOMPLETE", "NOT_READY"}
        assert not result.deployment_evidence_ready


def test_duplicate_unknown_excess_and_evidence_tampering_fail_closed() -> None:
    duplicate = [_item("WINDOWS_STARTUP_TASK_PROPOSAL")] * 2
    assert evaluate_supervisor_deployment_gate(duplicate).status == "TAMPERED"
    unknown = make_supervisor_deployment_evidence(
        component="UNKNOWN",
        artifact_hash="a" * 64,
        verified=True,
        complete=True,
        safety_proven=True,
        activation_disabled=True,
    )
    assert evaluate_supervisor_deployment_gate([unknown]).status == "TAMPERED"
    with pytest.raises(SupervisorDeploymentGateError, match="BOUND_EXCEEDED"):
        evaluate_supervisor_deployment_gate(_all(), max_records=8)
    with pytest.raises(SupervisorDeploymentGateError, match="HASH_MISMATCH"):
        evaluate_supervisor_deployment_gate(
            [replace(_item("WINDOWS_STARTUP_TASK_PROPOSAL"), verified=False)]
        )


def test_decision_authority_tampering_and_operational_surfaces_fail_closed() -> None:
    decision = evaluate_supervisor_deployment_gate(_all())
    with pytest.raises(SupervisorDeploymentGateError, match="HASH_MISMATCH"):
        validate_supervisor_deployment_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(SupervisorDeploymentGateError, match="SAFETY_BOUNDARY"):
        validate_supervisor_deployment_decision(replace(decision, task_activation_authorized=True))
    forbidden = {"open", "write", "run", "Popen", "subprocess", "spawn", "schtasks"}
    assert forbidden.isdisjoint(evaluate_supervisor_deployment_gate.__code__.co_names)
