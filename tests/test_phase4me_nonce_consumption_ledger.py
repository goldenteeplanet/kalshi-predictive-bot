from __future__ import annotations

import copy

import pytest

from scripts.local.phase4me_nonce_consumption_ledger import (
    _digest,
    make_record,
    plan_recovery,
    validate_ledger,
)

TOKEN = "a" * 64
NONCE = "b" * 64
RECEIPT = "c" * 64


def _append(records, operation, *, transaction="tx-1", token=TOKEN, nonce=NONCE, receipt=None):
    generation = len(records)
    previous = records[-1]["record_sha256"] if records else "0" * 64
    records.append(
        make_record(
            operation,
            generation=generation + 1,
            expected_generation=generation,
            transaction_id=transaction,
            token_id=token,
            nonce_sha256=nonce,
            receipt_sha256=receipt,
            occurred_at=f"2026-08-28T20:{generation:02d}:00Z",
            previous_record_sha256=previous,
        )
    )
    return records


def _prepared():
    return _append([], "PREPARE")


def _consumed():
    return _append(_prepared(), "DURABLE_CONSUME", receipt=RECEIPT)


def _committed():
    return _append(_consumed(), "COMMIT", receipt=RECEIPT)


def _validate(records, **kwargs):
    return validate_ledger(records, evaluated_at="2026-08-28T20:10:00Z", **kwargs)


def _rehash(row):
    row["record_sha256"] = _digest(
        {key: value for key, value in row.items() if key != "record_sha256"}
    )


def test_prepare_consume_commit_chain_is_deterministic() -> None:
    records = _committed()
    first = _validate(records)
    assert first == _validate(records)
    assert first["verdict"] == "PASS"
    assert first["transactions"]["tx-1"]["state"] == "COMMITTED"
    assert first["reserved_or_consumed_nonce_sha256"] == [NONCE]


def test_exact_duplicate_commit_is_idempotent() -> None:
    records = _committed()
    records.append(copy.deepcopy(records[-1]))
    result = _validate(records)
    assert result["verdict"] == "PASS"
    assert result["generation"] == 3
    assert result["accepted_record_count"] == 3


def test_concurrent_consumers_cannot_reserve_same_nonce() -> None:
    records = _prepared()
    _append(records, "PREPARE", transaction="tx-2")
    result = _validate(records)
    assert result["verdict"] == "REFUSE"
    assert any("NONCE_ALREADY_RESERVED_OR_CONSUMED" in error for error in result["errors"])


def test_prepare_crash_recovers_to_abort_but_nonce_stays_burned() -> None:
    validation = _validate(_prepared())
    recovery = plan_recovery(validation, occurred_at="2026-08-28T20:09:00Z")
    assert recovery["actions"][0]["record"]["operation"] == "ABORT"
    records = _prepared() + [recovery["actions"][0]["record"]]
    result = _validate(records)
    assert result["transactions"]["tx-1"]["state"] == "ABORTED"
    assert NONCE in result["reserved_or_consumed_nonce_sha256"]
    _append(records, "PREPARE", transaction="tx-2")
    assert _validate(records)["verdict"] == "REFUSE"


def test_durable_consume_crash_recovers_only_to_commit() -> None:
    records = _consumed()
    recovery = plan_recovery(_validate(records), occurred_at="2026-08-28T20:09:00Z")
    proposed = recovery["actions"][0]["record"]
    assert proposed["operation"] == "COMMIT"
    assert proposed["receipt_sha256"] == RECEIPT
    assert _validate(records + [proposed])["transactions"]["tx-1"]["state"] == "COMMITTED"


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda rows: rows[1].update(record_sha256="0" * 64), "RECORD_HASH_INVALID"),
        (lambda rows: rows[1].update(previous_record_sha256="0" * 64), "CHAIN_LINK_INVALID"),
        (lambda rows: rows[1].update(expected_generation=0), "CAS_GENERATION_MISMATCH"),
        (lambda rows: rows[1].update(generation=9), "GENERATION_SEQUENCE_INVALID"),
        (lambda rows: rows[1].update(token_id="d" * 64), "TOKEN_OR_NONCE_SUBSTITUTION"),
        (lambda rows: rows[2].update(receipt_sha256="d" * 64), "RECEIPT_SUBSTITUTION"),
    ],
)
def test_corruption_generation_and_substitution_are_refused(mutation, error: str) -> None:
    records = _committed()
    mutation(records)
    if error in {"TOKEN_OR_NONCE_SUBSTITUTION", "RECEIPT_SUBSTITUTION"}:
        _rehash(records[1 if "TOKEN" in error else 2])
    result = _validate(records)
    assert result["verdict"] == "REFUSE"
    assert any(error in value for value in result["errors"])


def test_reordering_is_refused() -> None:
    records = _committed()
    records[0], records[1] = records[1], records[0]
    assert _validate(records)["verdict"] == "REFUSE"


def test_anchor_detects_truncation_and_generation_rollback() -> None:
    complete = _committed()
    head = complete[-1]["record_sha256"]
    result = _validate(complete[:-1], expected_generation=3, expected_head_sha256=head)
    assert result["verdict"] == "REFUSE"
    assert "ANCHORED_GENERATION_MISMATCH" in result["errors"]
    assert "ANCHORED_HEAD_MISMATCH" in result["errors"]


def test_conflicting_replay_is_refused() -> None:
    records = _committed()
    conflict = copy.deepcopy(records[-1])
    conflict["occurred_at"] = "2026-08-28T20:04:00Z"
    _rehash(conflict)
    records.append(conflict)
    assert any("CONFLICTING_REPLAY" in error for error in _validate(records)["errors"])


def test_recovery_of_invalid_ledger_fails_closed() -> None:
    records = _prepared()
    records[0]["record_sha256"] = "0" * 64
    result = plan_recovery(_validate(records), occurred_at="2026-08-28T20:11:00Z")
    assert result["verdict"] == "REFUSE"
    assert result["actions"] == []


def test_model_has_no_persistence_execution_or_order_capability() -> None:
    safety = _validate(_committed())["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
