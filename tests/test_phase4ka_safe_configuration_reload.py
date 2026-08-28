from dataclasses import replace

import pytest

from kalshi_predictor.workstation.safe_configuration_reload import (
    ALLOWLISTED_FIELDS,
    SafeConfigurationReloadError,
    evaluate_safe_configuration_reload,
    make_configuration_reload_request,
    validate_configuration_reload_decision,
)


def _request(**overrides):
    fields = dict(
        current_configuration_hash="1" * 64,
        candidate_configuration_hash="2" * 64,
        signature_decision_hash="3" * 64,
        rollback_snapshot_hash="4" * 64,
        signature_status="VALID",
        changed_fields=("observation_interval_seconds",),
        supervisor_healthy=True,
        recovery_action_pending=False,
        atomic_swap_proven=True,
        rollback_proven=True,
        complete=True,
    )
    fields.update(overrides)
    return make_configuration_reload_request(**fields)


def test_signed_allowlisted_atomic_rollbackable_change_is_ready_plan_only() -> None:
    first = evaluate_safe_configuration_reload(_request())
    assert first == evaluate_safe_configuration_reload(_request())
    assert first.status == "READY" and first.reload_plan_ready
    assert not any(
        (
            first.reload_application_authorized,
            first.task_activation_authorized,
            first.restart_authorized,
        )
    )
    validate_configuration_reload_decision(first)


def test_all_allowlisted_fields_are_canonical_and_order_independent() -> None:
    fields = tuple(ALLOWLISTED_FIELDS)
    first = evaluate_safe_configuration_reload(_request(changed_fields=fields))
    second = evaluate_safe_configuration_reload(_request(changed_fields=tuple(reversed(fields))))
    assert first == second and first.status == "READY"


@pytest.mark.parametrize(
    "overrides",
    [
        {"signature_status": "INVALID"},
        {"supervisor_healthy": False},
        {"recovery_action_pending": True},
        {"atomic_swap_proven": False},
        {"rollback_proven": False},
    ],
)
def test_invalid_signature_unhealthy_pending_or_unrollbackable_reload_is_denied(overrides) -> None:
    assert evaluate_safe_configuration_reload(_request(**overrides)).status == "DENIED"


def test_no_change_forbidden_duplicate_and_changeset_contradictions_fail_closed() -> None:
    same = _request(candidate_configuration_hash="1" * 64, changed_fields=())
    assert evaluate_safe_configuration_reload(same).status == "NO_CHANGE"
    assert (
        evaluate_safe_configuration_reload(_request(changed_fields=("restart_budget",))).status
        == "TAMPERED"
    )
    assert (
        evaluate_safe_configuration_reload(
            _request(changed_fields=("alert_rate_limit", "alert_rate_limit"))
        ).status
        == "TAMPERED"
    )
    assert (
        evaluate_safe_configuration_reload(_request(candidate_configuration_hash="1" * 64)).status
        == "TAMPERED"
    )
    assert evaluate_safe_configuration_reload(_request(changed_fields=())).status == "TAMPERED"


def test_incomplete_malformed_request_and_tampering_fail_closed() -> None:
    assert evaluate_safe_configuration_reload(_request(complete=False)).status == "INCOMPLETE"
    with pytest.raises(SafeConfigurationReloadError, match="FIELD_INVALID"):
        _request(changed_fields=("Bad-Field",))
    request = _request()
    with pytest.raises(SafeConfigurationReloadError, match="REQUEST_HASH_MISMATCH"):
        evaluate_safe_configuration_reload(replace(request, supervisor_healthy=False))


def test_decision_authority_tampering_and_operational_surfaces_fail_closed() -> None:
    decision = evaluate_safe_configuration_reload(_request())
    with pytest.raises(SafeConfigurationReloadError, match="DECISION_HASH_MISMATCH"):
        validate_configuration_reload_decision(replace(decision, reload_plan_hash="f" * 64))
    with pytest.raises(SafeConfigurationReloadError, match="SAFETY_BOUNDARY"):
        validate_configuration_reload_decision(
            replace(decision, reload_application_authorized=True)
        )
    forbidden = {"open", "write", "replace", "run", "Popen", "subprocess", "spawn", "schtasks"}
    assert forbidden.isdisjoint(evaluate_safe_configuration_reload.__code__.co_names)
