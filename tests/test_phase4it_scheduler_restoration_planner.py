from dataclasses import replace

import pytest
from kalshi_predictor.workstation.recovery_action_capability_model import (
    evaluate_recovery_action_capability,
    make_recovery_action_request,
)
from kalshi_predictor.workstation.scheduler_restoration_planner import (
    SchedulerRestorationPlannerError,
    make_scheduler_restoration_context,
    plan_scheduler_restoration,
    validate_scheduler_restoration_plan,
)

TARGET = "c" * 64


def _capability(action="SCHEDULER_RESTORE"):
    timeout = 120 if action == "SCHEDULER_RESTORE" else 30
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
        scheduler_evidence_hash="d" * 64,
        writer_exclusivity_evidence_hash="e" * 64,
        target_id_hash=TARGET,
        scheduler_state=state,
        writer_exclusivity_proven=True,
        evidence_complete=True,
    )
    fields.update(overrides)
    return make_scheduler_restoration_context(**fields)


def test_inactive_and_failed_scheduler_produce_one_attempt_invariant_checked_plan() -> None:
    capability = _capability()
    for state in ("INACTIVE", "FAILED"):
        result = plan_scheduler_restoration(capability, _context(capability, state))
        assert result.status == "PLANNED" and result.maximum_attempts == 1
        assert result.steps == (
            "VERIFY_WRITER_EXCLUSIVITY",
            "RESTORE_USER_SCHEDULER",
            "VERIFY_SCHEDULER_HEARTBEAT",
            "VERIFY_PROTECTED_INVARIANTS",
        )
        assert not any(
            (
                result.recovery_authorized,
                result.scheduler_control_authorized,
                result.service_control_authorized,
                result.host_restart_authorized,
                result.execution_authorized,
            )
        )
        validate_scheduler_restoration_plan(result)


def test_active_is_no_action_unknown_and_unproven_exclusivity_are_denied() -> None:
    capability = _capability()
    first = plan_scheduler_restoration(capability, _context(capability, "ACTIVE"))
    second = plan_scheduler_restoration(capability, _context(capability, "ACTIVE"))
    assert first == second and first.status == "NO_ACTION"
    assert (
        plan_scheduler_restoration(capability, _context(capability, "UNKNOWN")).status == "DENIED"
    )
    assert (
        plan_scheduler_restoration(
            capability, _context(capability, writer_exclusivity_proven=False)
        ).status
        == "DENIED"
    )


def test_wrong_capability_incomplete_and_binding_mismatch_fail_closed() -> None:
    capability = _capability()
    wrong = _capability("DATABASE_READABILITY_CHECK")
    assert plan_scheduler_restoration(wrong, _context(wrong)).status == "DENIED"
    assert (
        plan_scheduler_restoration(capability, _context(capability, evidence_complete=False)).status
        == "INCOMPLETE"
    )
    mismatch = make_scheduler_restoration_context(
        capability_decision_hash="0" * 64,
        scheduler_evidence_hash="d" * 64,
        writer_exclusivity_evidence_hash="e" * 64,
        target_id_hash=TARGET,
        scheduler_state="INACTIVE",
        writer_exclusivity_proven=True,
        evidence_complete=True,
    )
    assert plan_scheduler_restoration(capability, mismatch).status == "TAMPERED"


def test_context_plan_safety_tampering_and_scheduler_surfaces_fail_closed() -> None:
    capability = _capability()
    context = _context(capability)
    with pytest.raises(SchedulerRestorationPlannerError, match="CONTEXT_HASH_MISMATCH"):
        plan_scheduler_restoration(capability, replace(context, scheduler_state="ACTIVE"))
    result = plan_scheduler_restoration(capability, context)
    with pytest.raises(SchedulerRestorationPlannerError, match="PLAN_HASH_MISMATCH"):
        validate_scheduler_restoration_plan(replace(result, steps=("FORGED",)))
    with pytest.raises(SchedulerRestorationPlannerError, match="SAFETY_BOUNDARY"):
        validate_scheduler_restoration_plan(replace(result, scheduler_control_authorized=True))
    forbidden = {
        "systemctl",
        "schtasks",
        "open",
        "run",
        "popen",
        "subprocess",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(plan_scheduler_restoration.__code__.co_names)
