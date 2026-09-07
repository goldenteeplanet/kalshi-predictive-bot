from dataclasses import replace

import pytest
from kalshi_predictor.workstation.database_readability_classifier import (
    DatabaseReadabilityClassifierError,
    classify_database_readability,
    make_database_readability_evidence,
    validate_database_readability_decision,
)


def _evidence(**overrides):
    fields = dict(
        probe_id_hash="a" * 64,
        observed_at_epoch_seconds=100,
        connection_opened=True,
        schema_readable=True,
        protected_query_readable=True,
        error_code="NONE",
        complete=True,
    )
    fields.update(overrides)
    return make_database_readability_evidence(**fields)


def test_coherent_success_proves_readability_without_authority() -> None:
    first = classify_database_readability(_evidence(), evaluated_at_epoch_seconds=100)
    second = classify_database_readability(_evidence(), evaluated_at_epoch_seconds=100)
    assert first == second and first.status == "READABLE" and first.database_readability_proven
    assert not first.operator_alert_required and not first.restart_eligible
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_database_readability_decision(first)


@pytest.mark.parametrize(
    "error",
    [
        "CONNECTION_REFUSED",
        "FILE_NOT_FOUND",
        "PERMISSION_DENIED",
        "QUERY_FAILED",
        "SCHEMA_UNREADABLE",
    ],
)
def test_known_database_failures_are_unreadable_and_never_restartable(error) -> None:
    result = classify_database_readability(
        _evidence(
            connection_opened=False,
            schema_readable=False,
            protected_query_readable=False,
            error_code=error,
        ),
        evaluated_at_epoch_seconds=100,
    )
    assert result.status == "UNREADABLE" and result.operator_alert_required
    assert result.restart_eligible is False and result.host_restart_authorized is False


def test_contradictory_unknown_incomplete_future_and_stale_evidence_fail_closed() -> None:
    contradictory = classify_database_readability(
        _evidence(error_code="QUERY_FAILED"), evaluated_at_epoch_seconds=100
    )
    unknown = classify_database_readability(
        _evidence(
            connection_opened=False,
            schema_readable=False,
            protected_query_readable=False,
            error_code="MYSTERY",
        ),
        evaluated_at_epoch_seconds=100,
    )
    incomplete = classify_database_readability(
        _evidence(complete=False), evaluated_at_epoch_seconds=100
    )
    future = classify_database_readability(
        _evidence(observed_at_epoch_seconds=101), evaluated_at_epoch_seconds=100
    )
    stale = classify_database_readability(_evidence(), evaluated_at_epoch_seconds=221)
    assert contradictory.status == unknown.status == stale.status == "UNKNOWN"
    assert incomplete.status == "INCOMPLETE" and future.status == "TAMPERED"


def test_exact_age_boundary_is_inclusive() -> None:
    assert (
        classify_database_readability(_evidence(), evaluated_at_epoch_seconds=220).status
        == "READABLE"
    )
    assert (
        classify_database_readability(_evidence(), evaluated_at_epoch_seconds=221).status
        == "UNKNOWN"
    )


def test_evidence_decision_safety_tampering_and_io_surfaces_fail_closed() -> None:
    with pytest.raises(DatabaseReadabilityClassifierError, match="EVIDENCE_HASH_MISMATCH"):
        classify_database_readability(
            replace(_evidence(), connection_opened=False), evaluated_at_epoch_seconds=100
        )
    result = classify_database_readability(_evidence(), evaluated_at_epoch_seconds=100)
    with pytest.raises(DatabaseReadabilityClassifierError, match="DECISION_HASH_MISMATCH"):
        validate_database_readability_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(DatabaseReadabilityClassifierError, match="SAFETY_BOUNDARY"):
        validate_database_readability_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "open",
        "connect",
        "execute",
        "query",
        "subprocess",
        "socket",
        "restart",
        "reboot",
        "shutdown",
        "order",
    }
    assert forbidden.isdisjoint(classify_database_readability.__code__.co_names)
