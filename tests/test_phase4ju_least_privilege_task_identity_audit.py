from dataclasses import replace

import pytest
from kalshi_predictor.workstation.least_privilege_task_identity_audit import (
    REQUIRED_RIGHTS,
    LeastPrivilegeTaskIdentityAuditError,
    evaluate_least_privilege_task_identity,
    make_task_identity_evidence,
    validate_task_identity_audit_decision,
)


def _evidence(**overrides):
    fields = dict(
        identity_hash="1" * 64,
        policy_snapshot_hash="2" * 64,
        account_type="STANDARD_USER",
        logon_type="BATCH",
        requested_rights=tuple(REQUIRED_RIGHTS),
        administrator=False,
        highest_privileges=False,
        interactive_logon=False,
        network_dependency=False,
        evidence_complete=True,
    )
    fields.update(overrides)
    return make_task_identity_evidence(**fields)


def test_exact_standard_user_rights_pass_deterministically_without_activation() -> None:
    first = evaluate_least_privilege_task_identity(_evidence())
    second = evaluate_least_privilege_task_identity(
        _evidence(requested_rights=tuple(reversed(sorted(REQUIRED_RIGHTS))))
    )
    assert first == second and first.status == "PASS" and first.least_privilege_proven
    assert not any(
        (first.task_activation_authorized, first.restart_authorized, first.execution_authorized)
    )
    validate_task_identity_audit_decision(first)


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_type": "ADMINISTRATOR"},
        {"logon_type": "INTERACTIVE"},
        {"administrator": True},
        {"highest_privileges": True},
        {"interactive_logon": True},
        {"network_dependency": True},
    ],
)
def test_elevated_interactive_or_network_identity_fails(overrides) -> None:
    result = evaluate_least_privilege_task_identity(_evidence(**overrides))
    assert result.status == "FAIL" and not result.least_privilege_proven


def test_missing_unknown_duplicate_and_incomplete_rights_fail_closed() -> None:
    missing = tuple(sorted(REQUIRED_RIGHTS))[1:]
    assert (
        evaluate_least_privilege_task_identity(_evidence(requested_rights=missing)).status == "FAIL"
    )
    assert (
        evaluate_least_privilege_task_identity(
            _evidence(requested_rights=(*REQUIRED_RIGHTS, "SHUTDOWN_HOST"))
        ).status
        == "TAMPERED"
    )
    right = sorted(REQUIRED_RIGHTS)[0]
    assert (
        evaluate_least_privilege_task_identity(_evidence(requested_rights=(right, right))).status
        == "TAMPERED"
    )
    assert (
        evaluate_least_privilege_task_identity(_evidence(evidence_complete=False)).status
        == "INCOMPLETE"
    )


def test_malformed_evidence_decision_and_authority_tampering_fail_closed() -> None:
    with pytest.raises(LeastPrivilegeTaskIdentityAuditError, match="FIELD_INVALID"):
        _evidence(identity_hash="bad")
    evidence = _evidence()
    with pytest.raises(LeastPrivilegeTaskIdentityAuditError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_least_privilege_task_identity(replace(evidence, administrator=True))
    decision = evaluate_least_privilege_task_identity(evidence)
    with pytest.raises(LeastPrivilegeTaskIdentityAuditError, match="DECISION_HASH_MISMATCH"):
        validate_task_identity_audit_decision(replace(decision, rights_set_hash="f" * 64))
    with pytest.raises(LeastPrivilegeTaskIdentityAuditError, match="SAFETY_BOUNDARY"):
        validate_task_identity_audit_decision(replace(decision, task_activation_authorized=True))


def test_audit_has_no_identity_mutation_or_operational_surface() -> None:
    forbidden = {"open", "write", "run", "Popen", "subprocess", "system", "spawn", "schtasks"}
    assert forbidden.isdisjoint(evaluate_least_privilege_task_identity.__code__.co_names)
