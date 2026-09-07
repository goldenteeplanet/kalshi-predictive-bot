from dataclasses import replace

import pytest
from kalshi_predictor.workstation.database_readability_recovery_planner import (
    DatabaseReadabilityRecoveryPlannerError,
    make_database_readability_recovery_context,
    plan_database_readability_recovery,
    validate_database_readability_recovery_plan,
)
from kalshi_predictor.workstation.recovery_action_capability_model import (
    evaluate_recovery_action_capability,
    make_recovery_action_request,
)

TARGET = "c" * 64


def _capability(action="DATABASE_READABILITY_CHECK"):
    timeout = 30 if action == "DATABASE_READABILITY_CHECK" else 60
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


def _context(capability, state="UNREADABLE", **overrides):
    fields = dict(
        capability_decision_hash=capability.decision_hash,
        classifier_decision_hash="d" * 64,
        target_id_hash=TARGET,
        readability_state=state,
        evidence_complete=True,
    )
    fields.update(overrides)
    return make_database_readability_recovery_context(**fields)


def test_unreadable_produces_read_only_diagnostic_escalation_plan() -> None:
    capability = _capability()
    result = plan_database_readability_recovery(capability, _context(capability))
    assert result.status == "PLANNED" and result.maximum_attempts == 1
    assert result.steps == (
        "VERIFY_DATABASE_PATH_METADATA",
        "RUN_READ_ONLY_INTEGRITY_CHECK",
        "CAPTURE_DIAGNOSTIC_RESULT",
        "ESCALATE_OPERATOR",
    )
    assert not any(
        (
            result.database_write_authorized,
            result.database_restore_authorized,
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    )
    validate_database_readability_recovery_plan(result)


def test_readable_is_no_action_unknown_tampered_and_incomplete_fail_closed() -> None:
    capability = _capability()
    first = plan_database_readability_recovery(capability, _context(capability, "READABLE"))
    second = plan_database_readability_recovery(capability, _context(capability, "READABLE"))
    assert first == second and first.status == "NO_ACTION"
    for state in ("UNKNOWN", "TAMPERED"):
        assert (
            plan_database_readability_recovery(capability, _context(capability, state)).status
            == "DENIED"
        )
    assert (
        plan_database_readability_recovery(capability, _context(capability, "INCOMPLETE")).status
        == "INCOMPLETE"
    )


def test_wrong_capability_incomplete_evidence_and_binding_mismatch_fail_closed() -> None:
    capability = _capability()
    wrong = _capability("WSL_WAKE")
    assert plan_database_readability_recovery(wrong, _context(wrong)).status == "DENIED"
    assert (
        plan_database_readability_recovery(
            capability, _context(capability, evidence_complete=False)
        ).status
        == "INCOMPLETE"
    )
    mismatch = make_database_readability_recovery_context(
        capability_decision_hash="0" * 64,
        classifier_decision_hash="d" * 64,
        target_id_hash=TARGET,
        readability_state="UNREADABLE",
        evidence_complete=True,
    )
    assert plan_database_readability_recovery(capability, mismatch).status == "TAMPERED"


def test_context_plan_safety_tampering_and_database_surfaces_fail_closed() -> None:
    capability = _capability()
    context = _context(capability)
    with pytest.raises(DatabaseReadabilityRecoveryPlannerError, match="CONTEXT_HASH_MISMATCH"):
        plan_database_readability_recovery(
            capability, replace(context, readability_state="READABLE")
        )
    result = plan_database_readability_recovery(capability, context)
    with pytest.raises(DatabaseReadabilityRecoveryPlannerError, match="PLAN_HASH_MISMATCH"):
        validate_database_readability_recovery_plan(replace(result, steps=("FORGED",)))
    with pytest.raises(DatabaseReadabilityRecoveryPlannerError, match="SAFETY_BOUNDARY"):
        validate_database_readability_recovery_plan(replace(result, database_write_authorized=True))
    forbidden = {
        "connect",
        "execute",
        "delete",
        "update",
        "insert",
        "migrate",
        "restore",
        "open",
        "run",
        "subprocess",
        "restart",
        "order",
    }
    assert forbidden.isdisjoint(plan_database_readability_recovery.__code__.co_names)
