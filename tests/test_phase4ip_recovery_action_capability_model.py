from dataclasses import replace

import pytest

from kalshi_predictor.workstation.recovery_action_capability_model import (
    ACTION_TIMEOUT_LIMITS,
    RecoveryActionCapabilityModelError,
    evaluate_recovery_action_capability,
    make_recovery_action_request,
    validate_recovery_action_capability_decision,
)


def _request(action="WSL_WAKE", **overrides):
    fields = dict(
        request_id_hash="a" * 64,
        incident_id_hash="b" * 64,
        action=action,
        target_id_hash="c" * 64,
        requested_timeout_seconds=ACTION_TIMEOUT_LIMITS.get(action, 10),
        requested_attempts=1,
        dry_run=True,
        complete=True,
    )
    fields.update(overrides)
    return make_recovery_action_request(**fields)


@pytest.mark.parametrize("action", sorted(ACTION_TIMEOUT_LIMITS))
def test_each_allowlisted_action_allows_planning_only_at_exact_bounds(action) -> None:
    result = evaluate_recovery_action_capability(_request(action))
    assert result.status == "CAPABLE" and result.planning_authorized
    assert (
        result.maximum_attempts == 1
        and result.maximum_timeout_seconds == ACTION_TIMEOUT_LIMITS[action]
    )
    assert not any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.wsl_shutdown_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    )
    validate_recovery_action_capability_decision(result)


def test_unknown_nondryrun_multiple_attempt_and_timeout_excess_are_denied() -> None:
    assert evaluate_recovery_action_capability(_request("UNKNOWN_ACTION")).status == "DENIED"
    assert evaluate_recovery_action_capability(_request(dry_run=False)).status == "DENIED"
    assert evaluate_recovery_action_capability(_request(requested_attempts=2)).status == "DENIED"
    assert (
        evaluate_recovery_action_capability(_request(requested_timeout_seconds=61)).status
        == "DENIED"
    )


def test_incomplete_and_tampered_requests_fail_closed() -> None:
    assert evaluate_recovery_action_capability(_request(complete=False)).status == "INCOMPLETE"
    with pytest.raises(RecoveryActionCapabilityModelError, match="REQUEST_HASH_MISMATCH"):
        evaluate_recovery_action_capability(replace(_request(), dry_run=False))


def test_decision_safety_tampering_and_operational_surfaces_fail_closed() -> None:
    result = evaluate_recovery_action_capability(_request())
    with pytest.raises(RecoveryActionCapabilityModelError, match="DECISION_HASH_MISMATCH"):
        validate_recovery_action_capability_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(RecoveryActionCapabilityModelError, match="SAFETY_BOUNDARY"):
        validate_recovery_action_capability_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "open",
        "run",
        "popen",
        "subprocess",
        "socket",
        "systemctl",
        "wsl",
        "shutdown",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(evaluate_recovery_action_capability.__code__.co_names)
