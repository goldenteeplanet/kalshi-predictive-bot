from dataclasses import replace

import pytest

from kalshi_predictor.workstation.installation_rollback_package import (
    REQUIRED_OPERATIONS,
    InstallationRollbackPackageError,
    evaluate_installation_rollback_package,
    make_rollback_package_request,
    validate_rollback_package_decision,
)


def _request(**overrides):
    fields = dict(
        installation_manifest_hash="1" * 64,
        startup_task_proposal_hash="2" * 64,
        installed_configuration_hash="3" * 64,
        previous_configuration_hash="4" * 64,
        incident_journal_hash="5" * 64,
        restart_history_hash="6" * 64,
        operations=REQUIRED_OPERATIONS,
        evidence_preservation_required=True,
        execution_requested=False,
        complete=True,
    )
    fields.update(overrides)
    return make_rollback_package_request(**fields)


def test_exact_rollback_package_is_deterministic_non_executable_and_preserves_evidence() -> None:
    first = evaluate_installation_rollback_package(_request())
    assert first == evaluate_installation_rollback_package(_request())
    assert first.status == "READY" and first.package_ready
    assert first.operations == REQUIRED_OPERATIONS and first.evidence_preserved
    assert not any(
        (
            first.rollback_execution_authorized,
            first.task_mutation_authorized,
            first.restart_authorized,
        )
    )
    validate_rollback_package_decision(first)


@pytest.mark.parametrize(
    "overrides",
    [
        {"operations": tuple(reversed(REQUIRED_OPERATIONS))},
        {"operations": REQUIRED_OPERATIONS[:-1]},
        {"installed_configuration_hash": "4" * 64},
        {"evidence_preservation_required": False},
        {"execution_requested": True},
    ],
)
def test_order_missing_snapshot_preservation_or_execution_drift_is_denied(overrides) -> None:
    result = evaluate_installation_rollback_package(_request(**overrides))
    assert result.status == "DENIED" and not result.package_ready


def test_duplicate_unknown_incomplete_malformed_and_request_tampering_fail_closed() -> None:
    assert (
        evaluate_installation_rollback_package(
            _request(operations=(*REQUIRED_OPERATIONS, REQUIRED_OPERATIONS[-1]))
        ).status
        == "TAMPERED"
    )
    assert (
        evaluate_installation_rollback_package(
            _request(operations=(*REQUIRED_OPERATIONS[:-1], "DELETE_EVIDENCE"))
        ).status
        == "TAMPERED"
    )
    assert evaluate_installation_rollback_package(_request(complete=False)).status == "INCOMPLETE"
    with pytest.raises(InstallationRollbackPackageError, match="FIELD_INVALID"):
        _request(incident_journal_hash="bad")
    with pytest.raises(InstallationRollbackPackageError, match="REQUEST_HASH_MISMATCH"):
        evaluate_installation_rollback_package(replace(_request(), execution_requested=True))


def test_decision_and_authority_tampering_fail_closed() -> None:
    decision = evaluate_installation_rollback_package(_request())
    with pytest.raises(InstallationRollbackPackageError, match="DECISION_HASH_MISMATCH"):
        validate_rollback_package_decision(replace(decision, package_manifest_hash="f" * 64))
    with pytest.raises(InstallationRollbackPackageError, match="SAFETY_BOUNDARY"):
        validate_rollback_package_decision(replace(decision, task_mutation_authorized=True))


def test_package_builder_has_no_delete_filesystem_task_or_execution_surface() -> None:
    forbidden = {
        "open",
        "write",
        "unlink",
        "remove",
        "run",
        "Popen",
        "subprocess",
        "spawn",
        "schtasks",
    }
    assert forbidden.isdisjoint(evaluate_installation_rollback_package.__code__.co_names)
