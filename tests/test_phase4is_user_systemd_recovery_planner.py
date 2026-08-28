from dataclasses import replace

import pytest

from kalshi_predictor.workstation.recovery_action_capability_model import (
    evaluate_recovery_action_capability,
    make_recovery_action_request,
)
from kalshi_predictor.workstation.user_systemd_recovery_planner import (
    UserSystemdRecoveryPlannerError,
    make_user_systemd_recovery_context,
    plan_user_systemd_recovery,
    validate_user_systemd_recovery_plan,
)

TARGET = "c" * 64


def _capability(action="USER_SYSTEMD_RECOVER"):
    timeout = 90 if action == "USER_SYSTEMD_RECOVER" else 30
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


def _context(capability, state="UNREACHABLE", **overrides):
    fields = dict(
        capability_decision_hash=capability.decision_hash,
        systemd_evidence_hash="d" * 64,
        target_id_hash=TARGET,
        scope="USER",
        manager_state=state,
        evidence_complete=True,
    )
    fields.update(overrides)
    return make_user_systemd_recovery_context(**fields)


def test_unreachable_and_degraded_user_manager_produce_one_attempt_symbolic_plan() -> None:
    capability = _capability()
    for state in ("UNREACHABLE", "DEGRADED"):
        result = plan_user_systemd_recovery(capability, _context(capability, state))
        assert result.status == "PLANNED" and result.maximum_attempts == 1
        assert result.steps == (
            "VERIFY_USER_SCOPE",
            "RESTORE_USER_MANAGER",
            "VERIFY_USER_MANAGER_REACHABILITY",
            "VERIFY_USER_UNITS",
        )
        assert not any(
            (
                result.recovery_authorized,
                result.service_control_authorized,
                result.system_scope_authorized,
                result.host_restart_authorized,
                result.execution_authorized,
            )
        )
        validate_user_systemd_recovery_plan(result)


def test_reachable_is_deterministic_no_action_unknown_and_system_scope_denied() -> None:
    capability = _capability()
    first = plan_user_systemd_recovery(capability, _context(capability, "REACHABLE"))
    second = plan_user_systemd_recovery(capability, _context(capability, "REACHABLE"))
    assert first == second and first.status == "NO_ACTION"
    assert (
        plan_user_systemd_recovery(capability, _context(capability, "UNKNOWN")).status == "DENIED"
    )
    assert (
        plan_user_systemd_recovery(capability, _context(capability, scope="SYSTEM")).status
        == "DENIED"
    )


def test_wrong_capability_incomplete_and_binding_mismatch_fail_closed() -> None:
    capability = _capability()
    wrong = _capability("DATABASE_READABILITY_CHECK")
    assert plan_user_systemd_recovery(wrong, _context(wrong)).status == "DENIED"
    assert (
        plan_user_systemd_recovery(capability, _context(capability, evidence_complete=False)).status
        == "INCOMPLETE"
    )
    mismatch = make_user_systemd_recovery_context(
        capability_decision_hash="0" * 64,
        systemd_evidence_hash="d" * 64,
        target_id_hash=TARGET,
        scope="USER",
        manager_state="UNREACHABLE",
        evidence_complete=True,
    )
    assert plan_user_systemd_recovery(capability, mismatch).status == "TAMPERED"


def test_context_plan_safety_tampering_and_systemctl_surfaces_fail_closed() -> None:
    capability = _capability()
    context = _context(capability)
    with pytest.raises(UserSystemdRecoveryPlannerError, match="CONTEXT_HASH_MISMATCH"):
        plan_user_systemd_recovery(capability, replace(context, manager_state="REACHABLE"))
    result = plan_user_systemd_recovery(capability, context)
    with pytest.raises(UserSystemdRecoveryPlannerError, match="PLAN_HASH_MISMATCH"):
        validate_user_systemd_recovery_plan(replace(result, steps=("FORGED",)))
    with pytest.raises(UserSystemdRecoveryPlannerError, match="SAFETY_BOUNDARY"):
        validate_user_systemd_recovery_plan(replace(result, service_control_authorized=True))
    forbidden = {
        "systemctl",
        "dbus",
        "loginctl",
        "open",
        "run",
        "popen",
        "subprocess",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(plan_user_systemd_recovery.__code__.co_names)
