from dataclasses import replace

import pytest
from kalshi_predictor.workstation.supervisor_crash_recovery import (
    SupervisorCrashRecoveryError,
    evaluate_supervisor_crash_recovery,
    make_supervisor_crash_evidence,
    validate_supervisor_crash_recovery_decision,
)


def _evidence(**overrides):
    fields = dict(
        lock_record_hash="1" * 64,
        owner_liveness_hash="2" * 64,
        restart_intent_hash="3" * 64,
        owner_status="LIVE",
        lock_valid=True,
        durable_intent_present=False,
        post_boot_verification_pending=False,
        evidence_complete=True,
        integrity_verified=True,
    )
    fields.update(overrides)
    return make_supervisor_crash_evidence(**fields)


def test_live_owner_is_healthy_and_kept_without_any_authority() -> None:
    first = evaluate_supervisor_crash_recovery(_evidence())
    assert first == evaluate_supervisor_crash_recovery(_evidence())
    assert first.status == "HEALTHY" and first.disposition == "KEEP_EXISTING_OWNER"
    assert not first.operator_reconciliation_required
    assert not any(
        (first.lock_release_authorized, first.lock_steal_authorized, first.restart_authorized)
    )
    validate_supervisor_crash_recovery_decision(first)


def test_dead_owner_with_durable_intent_requires_intent_and_lock_reconciliation() -> None:
    result = evaluate_supervisor_crash_recovery(
        _evidence(
            owner_status="DEAD",
            durable_intent_present=True,
            post_boot_verification_pending=True,
        )
    )
    assert result.status == "RECOVERY_REQUIRED"
    assert result.disposition == "RECONCILE_INTENT_AND_LOCK"
    assert result.operator_reconciliation_required and not result.automatic_replay_authorized


def test_dead_owner_without_intent_requires_lock_only_reconciliation() -> None:
    result = evaluate_supervisor_crash_recovery(_evidence(owner_status="DEAD"))
    assert result.status == "RECOVERY_REQUIRED"
    assert result.disposition == "RECONCILE_LOCK_ONLY"
    assert not result.lock_release_authorized and not result.lock_steal_authorized


def test_unknown_owner_or_untrusted_lock_state_denies() -> None:
    assert evaluate_supervisor_crash_recovery(_evidence(owner_status="UNKNOWN")).status == "DENIED"
    for overrides in (
        {"lock_valid": False},
        {"evidence_complete": False},
        {"integrity_verified": False},
    ):
        assert evaluate_supervisor_crash_recovery(_evidence(**overrides)).status == "DENIED"


def test_pending_post_boot_without_durable_intent_is_tampered() -> None:
    result = evaluate_supervisor_crash_recovery(
        _evidence(owner_status="DEAD", post_boot_verification_pending=True)
    )
    assert result.status == "TAMPERED" and not result.automatic_replay_authorized


def test_malformed_evidence_decision_and_authority_tampering_fail_closed() -> None:
    with pytest.raises(SupervisorCrashRecoveryError, match="FIELD_INVALID"):
        _evidence(owner_status="MAYBE")
    evidence = _evidence()
    with pytest.raises(SupervisorCrashRecoveryError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_supervisor_crash_recovery(replace(evidence, owner_status="DEAD"))
    decision = evaluate_supervisor_crash_recovery(evidence)
    with pytest.raises(SupervisorCrashRecoveryError, match="DECISION_HASH_MISMATCH"):
        validate_supervisor_crash_recovery_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(SupervisorCrashRecoveryError, match="SAFETY_BOUNDARY"):
        validate_supervisor_crash_recovery_decision(replace(decision, lock_steal_authorized=True))


def test_recovery_evaluator_has_no_lock_mutation_or_operational_surface() -> None:
    forbidden = {
        "open",
        "write",
        "unlink",
        "remove",
        "run",
        "Popen",
        "subprocess",
        "system",
        "spawn",
    }
    assert forbidden.isdisjoint(evaluate_supervisor_crash_recovery.__code__.co_names)
