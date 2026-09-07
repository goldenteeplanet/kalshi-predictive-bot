from dataclasses import replace

import pytest
from kalshi_predictor.workstation.keepalive_restoration_dry_run_planner import (
    KeepaliveRestorationDryRunPlannerError,
    make_keepalive_restoration_context,
    plan_keepalive_restoration_dry_run,
    validate_keepalive_restoration_dry_run_plan,
)
from kalshi_predictor.workstation.recovery_action_capability_model import (
    evaluate_recovery_action_capability,
    make_recovery_action_request,
)

TARGET = "c" * 64


def _capability(action="KEEPALIVE_RESTORE"):
    timeout = 60 if action == "KEEPALIVE_RESTORE" else 30
    request = make_recovery_action_request(
        request_id_hash="a" * 64,
        incident_id_hash="b" * 64,
        action=action,
        target_id_hash=TARGET,
        requested_timeout_seconds=timeout,
        requested_attempts=1,
        dry_run=True,
        complete=True,
    )
    return evaluate_recovery_action_capability(request)


def _context(capability, state="INACTIVE", **overrides):
    fields = dict(
        capability_decision_hash=capability.decision_hash,
        health_evidence_hash="d" * 64,
        target_id_hash=TARGET,
        current_state=state,
        evidence_complete=True,
    )
    fields.update(overrides)
    return make_keepalive_restoration_context(**fields)


def test_inactive_and_failed_states_produce_one_attempt_symbolic_plan() -> None:
    capability = _capability()
    for state in ("INACTIVE", "FAILED"):
        result = plan_keepalive_restoration_dry_run(capability, _context(capability, state))
        assert result.status == "PLANNED" and result.maximum_attempts == 1
        assert result.steps == (
            "VERIFY_KEEPALIVE_NOT_ACTIVE",
            "RESTORE_KEEPALIVE_TASK",
            "VERIFY_KEEPALIVE_HEARTBEAT",
        )
        assert not any(
            (
                result.recovery_authorized,
                result.task_control_authorized,
                result.service_control_authorized,
                result.host_restart_authorized,
                result.execution_authorized,
            )
        )
        validate_keepalive_restoration_dry_run_plan(result)


def test_active_is_deterministic_no_action_and_unknown_is_denied() -> None:
    capability = _capability()
    first = plan_keepalive_restoration_dry_run(capability, _context(capability, "ACTIVE"))
    second = plan_keepalive_restoration_dry_run(capability, _context(capability, "ACTIVE"))
    assert first == second and first.status == "NO_ACTION"
    assert (
        plan_keepalive_restoration_dry_run(capability, _context(capability, "UNKNOWN")).status
        == "DENIED"
    )


def test_wrong_capability_incomplete_and_binding_mismatch_fail_closed() -> None:
    capability = _capability()
    wrong = _capability("DATABASE_READABILITY_CHECK")
    assert plan_keepalive_restoration_dry_run(wrong, _context(wrong)).status == "DENIED"
    assert (
        plan_keepalive_restoration_dry_run(
            capability, _context(capability, evidence_complete=False)
        ).status
        == "INCOMPLETE"
    )
    mismatch = make_keepalive_restoration_context(
        capability_decision_hash="0" * 64,
        health_evidence_hash="d" * 64,
        target_id_hash=TARGET,
        current_state="INACTIVE",
        evidence_complete=True,
    )
    assert plan_keepalive_restoration_dry_run(capability, mismatch).status == "TAMPERED"


def test_context_plan_safety_tampering_and_task_surfaces_fail_closed() -> None:
    capability = _capability()
    context = _context(capability)
    with pytest.raises(KeepaliveRestorationDryRunPlannerError, match="CONTEXT_HASH_MISMATCH"):
        plan_keepalive_restoration_dry_run(capability, replace(context, current_state="ACTIVE"))
    result = plan_keepalive_restoration_dry_run(capability, context)
    with pytest.raises(KeepaliveRestorationDryRunPlannerError, match="PLAN_HASH_MISMATCH"):
        validate_keepalive_restoration_dry_run_plan(replace(result, steps=("FORGED",)))
    with pytest.raises(KeepaliveRestorationDryRunPlannerError, match="SAFETY_BOUNDARY"):
        validate_keepalive_restoration_dry_run_plan(replace(result, task_control_authorized=True))
    forbidden = {
        "schtasks",
        "start-scheduledtask",
        "open",
        "run",
        "popen",
        "subprocess",
        "systemctl",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(plan_keepalive_restoration_dry_run.__code__.co_names)
