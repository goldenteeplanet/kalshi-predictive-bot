from dataclasses import replace

import pytest

from kalshi_predictor.workstation.database_readability_classifier import (
    classify_database_readability,
    make_database_readability_evidence,
)
from kalshi_predictor.workstation.post_boot_database_readability_verification import (
    PostBootDatabaseReadabilityError,
    evaluate_post_boot_database_readability,
    validate_post_boot_database_readability_decision,
)


def _database(**overrides):
    fields = dict(
        probe_id_hash="a" * 64,
        observed_at_epoch_seconds=1_000,
        connection_opened=True,
        schema_readable=True,
        protected_query_readable=True,
        error_code="NONE",
        complete=True,
    )
    fields.update(overrides)
    evidence = make_database_readability_evidence(**fields)
    return classify_database_readability(evidence, evaluated_at_epoch_seconds=1_010)


def _evaluate(database=None, **overrides):
    fields = dict(
        restart_intent_hash="1" * 64,
        scheduler_decision_hash="2" * 64,
        scheduler_verified=True,
        database_decision=database or _database(),
    )
    fields.update(overrides)
    return evaluate_post_boot_database_readability(**fields)


def test_readable_database_passes_deterministically_without_authority() -> None:
    first = _evaluate()
    assert first == _evaluate()
    assert first.status == "PASS" and first.post_boot_database_verified
    assert first.post_boot_chain_may_continue and not first.database_write_authorized
    validate_post_boot_database_readability_decision(first)


def test_scheduler_prerequisite_blocks_database_chain() -> None:
    result = _evaluate(scheduler_verified=False)
    assert result.status == "INCOMPLETE" and not result.post_boot_chain_may_continue


def test_unreadable_database_fails_closed() -> None:
    result = _evaluate(
        _database(
            connection_opened=False,
            schema_readable=False,
            protected_query_readable=False,
            error_code="CONNECTION_REFUSED",
        )
    )
    assert result.status == "FAIL"
    assert "CONNECTION_REFUSED" in result.reasons[0]


def test_unknown_incomplete_and_tampered_are_not_accepted() -> None:
    contradictory = _database(error_code="QUERY_FAILED")
    incomplete = _database(complete=False)
    future_evidence = make_database_readability_evidence(
        probe_id_hash="a" * 64,
        observed_at_epoch_seconds=1_011,
        connection_opened=True,
        schema_readable=True,
        protected_query_readable=True,
        error_code="NONE",
        complete=True,
    )
    future = classify_database_readability(future_evidence, evaluated_at_epoch_seconds=1_010)
    assert _evaluate(contradictory).status == "INCOMPLETE"
    assert _evaluate(incomplete).status == "INCOMPLETE"
    assert _evaluate(future).status == "TAMPERED"


def test_invalid_upstream_decision_and_hash_fields_fail_closed() -> None:
    with pytest.raises(PostBootDatabaseReadabilityError, match="EVIDENCE_INVALID"):
        _evaluate(replace(_database(), database_readability_proven=False))
    with pytest.raises(PostBootDatabaseReadabilityError, match="FIELD_INVALID"):
        _evaluate(scheduler_decision_hash="bad")


def test_decision_tampering_and_io_surfaces_fail_closed() -> None:
    decision = _evaluate()
    with pytest.raises(PostBootDatabaseReadabilityError, match="DECISION_HASH_MISMATCH"):
        validate_post_boot_database_readability_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(PostBootDatabaseReadabilityError, match="SAFETY_BOUNDARY"):
        validate_post_boot_database_readability_decision(
            replace(decision, database_write_authorized=True)
        )
    forbidden = {
        "open",
        "connect",
        "execute",
        "query",
        "write",
        "subprocess",
        "restart",
        "shutdown",
    }
    assert forbidden.isdisjoint(evaluate_post_boot_database_readability.__code__.co_names)
