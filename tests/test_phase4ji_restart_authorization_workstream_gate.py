from dataclasses import replace

import pytest
from kalshi_predictor.workstation.restart_authorization_workstream_gate import (
    REQUIRED_COMPONENTS,
    RestartAuthorizationWorkstreamGateError,
    evaluate_restart_authorization_workstream_gate,
    make_restart_authorization_component_evidence,
    validate_restart_authorization_workstream_decision,
)


def _item(component, **overrides):
    fields = dict(
        component=component,
        artifact_hash=f"{sorted(REQUIRED_COMPONENTS).index(component) + 1:064x}",
        verified=True,
        complete=True,
        safety_proven=True,
        dry_run_or_read_only=True,
    )
    fields.update(overrides)
    return make_restart_authorization_component_evidence(**fields)


def _all():
    return [_item(component) for component in REQUIRED_COMPONENTS]


def test_all_nine_components_make_deterministic_ready_but_non_authorizing_gate() -> None:
    first = evaluate_restart_authorization_workstream_gate(_all())
    second = evaluate_restart_authorization_workstream_gate(list(reversed(_all())))
    assert first == second and first.status == "READY"
    assert first.restart_authorization_workstream_ready and first.component_count == 9
    assert first.downstream_simulation_required and first.operator_activation_required
    assert not any(
        (
            first.restart_authorized,
            first.process_spawn_authorized,
            first.service_control_authorized,
            first.execution_authorized,
        )
    )
    validate_restart_authorization_workstream_decision(first)


def test_each_missing_component_is_incomplete() -> None:
    for missing in REQUIRED_COMPONENTS:
        result = evaluate_restart_authorization_workstream_gate(
            [item for item in _all() if item.component != missing]
        )
        assert result.status == "INCOMPLETE"
        assert f"RESTART_AUTHORIZATION_COMPONENT_MISSING:{missing}" in result.reasons


def test_incomplete_unverified_unsafe_or_non_dry_run_components_block() -> None:
    for field in ("complete", "verified", "safety_proven", "dry_run_or_read_only"):
        records = _all()
        records[0] = _item(records[0].component, **{field: False})
        result = evaluate_restart_authorization_workstream_gate(records)
        assert result.status in {"INCOMPLETE", "NOT_READY"}
        assert not result.restart_authorization_workstream_ready


def test_duplicate_unknown_excess_and_evidence_tampering_fail_closed() -> None:
    duplicate = [_item("HOST_RESTART_ELIGIBILITY_MODEL")] * 2
    assert evaluate_restart_authorization_workstream_gate(duplicate).status == "TAMPERED"
    unknown = make_restart_authorization_component_evidence(
        component="UNKNOWN",
        artifact_hash="a" * 64,
        verified=True,
        complete=True,
        safety_proven=True,
        dry_run_or_read_only=True,
    )
    assert evaluate_restart_authorization_workstream_gate([unknown]).status == "TAMPERED"
    with pytest.raises(RestartAuthorizationWorkstreamGateError, match="BOUND_EXCEEDED"):
        evaluate_restart_authorization_workstream_gate(_all(), max_records=8)
    with pytest.raises(RestartAuthorizationWorkstreamGateError, match="HASH_MISMATCH"):
        evaluate_restart_authorization_workstream_gate(
            [replace(_item("HOST_RESTART_ELIGIBILITY_MODEL"), verified=False)]
        )


def test_decision_and_authority_tampering_fail_closed_without_operational_surfaces() -> None:
    decision = evaluate_restart_authorization_workstream_gate(_all())
    with pytest.raises(RestartAuthorizationWorkstreamGateError, match="HASH_MISMATCH"):
        validate_restart_authorization_workstream_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(RestartAuthorizationWorkstreamGateError, match="SAFETY_BOUNDARY"):
        validate_restart_authorization_workstream_decision(
            replace(decision, restart_authorized=True)
        )
    forbidden = {"open", "run", "Popen", "subprocess", "system", "spawn", "socket"}
    assert forbidden.isdisjoint(evaluate_restart_authorization_workstream_gate.__code__.co_names)
