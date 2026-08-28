from dataclasses import replace

import pytest

from kalshi_predictor.workstation.recovery_action_capability_model import (
    evaluate_recovery_action_capability,
    make_recovery_action_request,
)
from kalshi_predictor.workstation.wsl_status_evidence_capture import (
    capture_wsl_status_evidence,
    make_wsl_distribution_evidence,
)
from kalshi_predictor.workstation.wsl_wake_dry_run_planner import (
    WslWakeDryRunPlannerError,
    make_wsl_wake_context,
    plan_wsl_wake_dry_run,
    validate_wsl_wake_dry_run_plan,
)

TARGET = "c" * 64


def _capability(action="WSL_WAKE"):
    request = make_recovery_action_request(
        request_id_hash="a" * 64,
        incident_id_hash="b" * 64,
        action=action,
        target_id_hash=TARGET,
        requested_timeout_seconds=60 if action == "WSL_WAKE" else 30,
        requested_attempts=1,
        dry_run=True,
        complete=True,
    )
    return evaluate_recovery_action_capability(request)


def _status():
    distro = make_wsl_distribution_evidence(
        distribution_id_hash=TARGET,
        state="STOPPED",
        wsl_version=2,
        is_default=True,
        observed_at_epoch_seconds=100,
        complete=True,
    )
    return capture_wsl_status_evidence([distro], captured_at_epoch_seconds=100)


def _context(capability, status, state="STOPPED", **overrides):
    fields = dict(
        capability_decision_hash=capability.decision_hash,
        wsl_status_capture_hash=status.capture_hash,
        target_distribution_id_hash=TARGET,
        current_state=state,
        complete=True,
    )
    fields.update(overrides)
    return make_wsl_wake_context(**fields)


def test_stopped_target_produces_deterministic_symbolic_plan_without_authority() -> None:
    capability, status = _capability(), _status()
    first = plan_wsl_wake_dry_run(capability, status, _context(capability, status))
    second = plan_wsl_wake_dry_run(capability, status, _context(capability, status))
    assert first == second and first.status == "PLANNED"
    assert first.steps == (
        "VERIFY_TARGET_STOPPED",
        "WAKE_TARGET_DISTRIBUTION",
        "VERIFY_TARGET_LIVENESS",
    )
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.wsl_wake_authorized,
            first.wsl_shutdown_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_wsl_wake_dry_run_plan(first)


def test_running_is_no_action_and_nonwakeable_states_are_denied() -> None:
    capability, status = _capability(), _status()
    assert (
        plan_wsl_wake_dry_run(capability, status, _context(capability, status, "RUNNING")).status
        == "NO_ACTION"
    )
    for state in ("INSTALLING", "UNAVAILABLE", "UNKNOWN"):
        assert (
            plan_wsl_wake_dry_run(capability, status, _context(capability, status, state)).status
            == "DENIED"
        )


def test_wrong_capability_incomplete_and_binding_mismatch_fail_closed() -> None:
    capability, status = _capability(), _status()
    wrong = _capability("DATABASE_READABILITY_CHECK")
    assert plan_wsl_wake_dry_run(wrong, status, _context(wrong, status)).status == "DENIED"
    assert (
        plan_wsl_wake_dry_run(
            capability, status, _context(capability, status, complete=False)
        ).status
        == "INCOMPLETE"
    )
    mismatch = make_wsl_wake_context(
        capability_decision_hash="0" * 64,
        wsl_status_capture_hash=status.capture_hash,
        target_distribution_id_hash=TARGET,
        current_state="STOPPED",
        complete=True,
    )
    assert plan_wsl_wake_dry_run(capability, status, mismatch).status == "TAMPERED"


def test_context_plan_safety_tampering_and_wsl_surfaces_fail_closed() -> None:
    capability, status = _capability(), _status()
    context = _context(capability, status)
    with pytest.raises(WslWakeDryRunPlannerError, match="CONTEXT_HASH_MISMATCH"):
        plan_wsl_wake_dry_run(capability, status, replace(context, current_state="RUNNING"))
    result = plan_wsl_wake_dry_run(capability, status, context)
    with pytest.raises(WslWakeDryRunPlannerError, match="PLAN_HASH_MISMATCH"):
        validate_wsl_wake_dry_run_plan(replace(result, steps=("FORGED",)))
    with pytest.raises(WslWakeDryRunPlannerError, match="SAFETY_BOUNDARY"):
        validate_wsl_wake_dry_run_plan(replace(result, wsl_wake_authorized=True))
    forbidden = {
        "wsl",
        "wsl.exe",
        "open",
        "run",
        "popen",
        "subprocess",
        "shutdown",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(plan_wsl_wake_dry_run.__code__.co_names)
