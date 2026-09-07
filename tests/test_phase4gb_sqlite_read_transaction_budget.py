from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.phase4cd.settled_count_contention_audit import (
    audit_settled_count_contention,
    make_query_sample,
)
from kalshi_predictor.phase4cd.sqlite_read_transaction_budget import (
    SQLiteReadTransactionBudgetError,
    build_sqlite_read_transaction_budget,
    validate_sqlite_read_transaction_budget,
)


def test_valid_clear_audit_grants_strict_read_budget() -> None:
    budget = _budget()
    validate_sqlite_read_transaction_budget(budget)
    assert budget.decision == "GRANT"
    assert budget.query_only is True
    assert budget.immutable_source is True
    assert budget.busy_timeout_ms == 0
    assert budget.execution_authorized is False


def test_empty_and_partial_inputs_fail_closed() -> None:
    with pytest.raises(SQLiteReadTransactionBudgetError, match="AUDIT_INPUT_INVALID"):
        build_sqlite_read_transaction_budget(
            audit=None, requested_rows=1, requested_duration_ms=100
        )
    with pytest.raises(SQLiteReadTransactionBudgetError, match="ROW_BUDGET_EMPTY"):
        build_sqlite_read_transaction_budget(
            audit=_audit(), requested_rows=0, requested_duration_ms=100
        )


def test_exact_row_duration_and_age_boundaries_grant() -> None:
    budget = _budget(age=300, rows=1, duration=100)
    assert budget.decision == "GRANT"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"rows": 2}, "ROW_BUDGET_EXCEEDED"),
        ({"duration": 101}, "DURATION_BUDGET_EXCEEDED"),
        ({"age": 301}, "AUDIT_EVIDENCE_STALE"),
        ({"busy_events": 1}, "AUDIT_NOT_CLEAR"),
    ],
)
def test_budget_breaches_deny(kwargs: dict[str, int], reason: str) -> None:
    budget = _budget(**kwargs)
    assert budget.decision == "DENY"
    assert reason in budget.reasons


def test_malformed_bounds_and_tampered_audit_fail_closed() -> None:
    with pytest.raises(SQLiteReadTransactionBudgetError, match="BUDGET_FIELD_INVALID"):
        build_sqlite_read_transaction_budget(
            audit=_audit(), requested_rows=1, requested_duration_ms=-1
        )
    audit = _audit()
    with pytest.raises(SQLiteReadTransactionBudgetError, match="AUDIT_INPUT_INVALID"):
        build_sqlite_read_transaction_budget(
            audit=replace(audit, audit_hash="0" * 64),
            requested_rows=1,
            requested_duration_ms=100,
        )


def test_result_tampering_and_read_only_contract_fail_closed() -> None:
    budget = _budget()
    with pytest.raises(SQLiteReadTransactionBudgetError, match="BUDGET_HASH_MISMATCH"):
        validate_sqlite_read_transaction_budget(replace(budget, budget_hash="0" * 64))
    with pytest.raises(SQLiteReadTransactionBudgetError, match="READ_ONLY_CONTRACT_INVALID"):
        validate_sqlite_read_transaction_budget(replace(budget, query_only=False))
    with pytest.raises(SQLiteReadTransactionBudgetError, match="BUDGET_SAFETY_BOUNDARY_INVALID"):
        validate_sqlite_read_transaction_budget(replace(budget, execution_authorized=True))


def test_budget_builder_has_no_sqlite_or_mutation_surface() -> None:
    names = set(build_sqlite_read_transaction_budget.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "execute", "open", "replace", "sqlite3", "unlink"}
    )


def _audit(*, age: int = 1, busy_events: int = 0):
    sample = make_query_sample(
        query_fingerprint="sha256:settled-count-v1",
        source_identity_hash="a" * 64,
        source_watermark="paper_pnl:204",
        age_seconds=age,
        duration_ms=20,
        busy_events=busy_events,
        result_count=203,
    )
    return audit_settled_count_contention(
        [sample], max_age_seconds=max(age, 300), max_duration_ms=250
    )


def _budget(*, age: int = 1, rows: int = 1, duration: int = 100, busy_events: int = 0):
    return build_sqlite_read_transaction_budget(
        audit=_audit(age=age, busy_events=busy_events),
        requested_rows=rows,
        requested_duration_ms=duration,
        max_rows=1,
        max_duration_ms=100,
        max_evidence_age_seconds=300,
    )
