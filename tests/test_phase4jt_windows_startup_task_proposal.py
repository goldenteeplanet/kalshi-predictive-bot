from dataclasses import replace

import pytest
from kalshi_predictor.workstation.windows_startup_task_proposal import (
    WindowsStartupTaskProposalError,
    evaluate_windows_startup_task_proposal,
    make_startup_task_proposal_request,
    validate_startup_task_proposal_decision,
)


def _request(**overrides):
    fields = dict(
        proposal_id_hash="1" * 64,
        supervisor_artifact_hash="2" * 64,
        configuration_hash="3" * 64,
        rollback_script_hash="4" * 64,
        task_name="Kalshi Paper Recovery Supervisor",
        trigger="AT_STARTUP",
        run_with_highest_privileges=False,
        network_required=False,
        activation_requested=False,
        dry_run=True,
        complete=True,
    )
    fields.update(overrides)
    return make_startup_task_proposal_request(**fields)


def test_least_privilege_startup_proposal_is_deterministic_and_disabled() -> None:
    first = evaluate_windows_startup_task_proposal(_request())
    assert first == evaluate_windows_startup_task_proposal(_request())
    assert first.status == "READY" and first.proposal_ready
    assert first.activation_disabled and first.operator_review_required
    assert not any(
        (first.task_creation_authorized, first.restart_authorized, first.execution_authorized)
    )
    validate_startup_task_proposal_decision(first)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"trigger": "AT_LOGON"}, "TRIGGER_NOT_ALLOWLISTED"),
        ({"run_with_highest_privileges": True}, "HIGHEST_PRIVILEGES_REFUSED"),
        ({"network_required": True}, "NETWORK_DEPENDENCY_REFUSED"),
        ({"activation_requested": True}, "ACTIVATION_REFUSED"),
        ({"dry_run": False}, "DRY_RUN_REQUIRED"),
    ],
)
def test_unsafe_or_active_proposal_requests_are_denied(overrides, reason) -> None:
    result = evaluate_windows_startup_task_proposal(_request(**overrides))
    assert result.status == "DENIED" and not result.proposal_ready
    assert any(reason in item for item in result.reasons)


def test_incomplete_malformed_and_request_tampering_fail_closed() -> None:
    assert evaluate_windows_startup_task_proposal(_request(complete=False)).status == "INCOMPLETE"
    with pytest.raises(WindowsStartupTaskProposalError, match="FIELD_INVALID"):
        _request(task_name="bad/task")
    with pytest.raises(WindowsStartupTaskProposalError, match="REQUEST_HASH_MISMATCH"):
        evaluate_windows_startup_task_proposal(replace(_request(), activation_requested=True))


def test_decision_and_authority_tampering_fail_closed() -> None:
    decision = evaluate_windows_startup_task_proposal(_request())
    with pytest.raises(WindowsStartupTaskProposalError, match="DECISION_HASH_MISMATCH"):
        validate_startup_task_proposal_decision(replace(decision, rollback_preview_hash="f" * 64))
    with pytest.raises(WindowsStartupTaskProposalError, match="SAFETY_BOUNDARY"):
        validate_startup_task_proposal_decision(replace(decision, task_creation_authorized=True))


def test_proposal_has_no_task_creation_process_or_operational_surface() -> None:
    forbidden = {"open", "write", "run", "Popen", "subprocess", "system", "spawn", "schtasks"}
    assert forbidden.isdisjoint(evaluate_windows_startup_task_proposal.__code__.co_names)
