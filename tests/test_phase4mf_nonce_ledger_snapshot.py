from __future__ import annotations

import copy

import pytest

from scripts.local.phase4me_nonce_consumption_ledger import validate_ledger
from scripts.local.phase4mf_nonce_ledger_snapshot import (
    POLICY_VERSION,
    _body_hash,
    audit_snapshot_chain,
    make_snapshot,
    nonce_replay_verdict,
    plan_compaction,
    validate_snapshot,
)
from tests.test_phase4me_nonce_consumption_ledger import NONCE, _committed, _consumed, _prepared


def _source(records):
    return validate_ledger(records, evaluated_at="2026-08-28T20:10:00Z")


def _snapshot(records=None, prior="0" * 64):
    return make_snapshot(_source(records or _committed()), prior_snapshot_sha256=prior)


def _validate(snapshot, records=None, prior="0" * 64):
    return validate_snapshot(
        snapshot,
        _source(records or _committed()),
        expected_prior_snapshot_sha256=prior,
    )


def _rehash(snapshot):
    snapshot["snapshot_sha256"] = _body_hash(snapshot)


def test_snapshot_is_deterministic_and_preserves_replay_refusal() -> None:
    snapshot = _snapshot()
    assert snapshot == _snapshot()
    validation = _validate(snapshot)
    assert validation["verdict"] == "PASS"
    assert nonce_replay_verdict(validation, NONCE) == "REFUSE_NONCE_RESERVED_OR_CONSUMED"
    assert (
        nonce_replay_verdict(validation, "f" * 64)
        == "NOT_FOUND_REQUIRES_AUTHORITATIVE_LEDGER_CHECK"
    )


def test_terminal_history_is_compactable_only_as_an_inert_plan() -> None:
    records = _committed()
    snapshot = _snapshot(records)
    result = plan_compaction(
        records,
        snapshot,
        evaluated_at="2026-08-28T20:10:00Z",
        expected_prior_snapshot_sha256="0" * 64,
    )
    assert result["verdict"] == "PASS"
    assert result["delete_through_generation"] == 3
    assert result["execution_performed"] is False
    assert result["replay_refusal_nonce_sha256"] == [NONCE]


@pytest.mark.parametrize("records", [_prepared(), _consumed()])
def test_in_flight_history_is_never_deletable(records) -> None:
    snapshot = _snapshot(records)
    result = plan_compaction(
        records,
        snapshot,
        evaluated_at="2026-08-28T20:10:00Z",
        expected_prior_snapshot_sha256="0" * 64,
    )
    assert result["verdict"] == "PASS"
    assert result["delete_through_generation"] == 0
    assert result["reason"] == "IN_FLIGHT_HISTORY_REQUIRED"
    assert result["recovery_transactions"] == snapshot["transactions"]


@pytest.mark.parametrize(
    "mutation,error",
    [
        (
            lambda row: row.update(reserved_or_consumed_nonce_sha256=[]),
            "RESERVED_OR_CONSUMED_NONCE_SHA256_BINDING_MISMATCH",
        ),
        (
            lambda row: row["transactions"]["tx-1"].update(state="ABORTED"),
            "TRANSACTIONS_BINDING_MISMATCH",
        ),
        (lambda row: row.update(source_generation=1), "SOURCE_GENERATION_BINDING_MISMATCH"),
        (
            lambda row: row.update(source_head_sha256="f" * 64),
            "SOURCE_HEAD_SHA256_BINDING_MISMATCH",
        ),
        (lambda row: row.update(compaction_policy_version="evil.v1"), "POLICY_VERSION_MISMATCH"),
        (lambda row: row.update(prior_snapshot_sha256="f" * 64), "PRIOR_ANCHOR_MISMATCH"),
    ],
)
def test_rehashed_omission_state_anchor_and_policy_mutations_fail(mutation, error: str) -> None:
    snapshot = _snapshot()
    mutation(snapshot)
    _rehash(snapshot)
    result = _validate(snapshot)
    assert result["verdict"] == "REFUSE"
    assert error in result["errors"]


def test_snapshot_substitution_against_different_source_fails() -> None:
    snapshot = _snapshot(_committed())
    result = _validate(snapshot, _prepared())
    assert result["verdict"] == "REFUSE"
    assert "SOURCE_GENERATION_BINDING_MISMATCH" in result["errors"]


def test_invalid_or_truncated_source_cannot_authorize_compaction() -> None:
    records = _committed()
    snapshot = _snapshot(records)
    result = plan_compaction(
        records[:-1],
        snapshot,
        evaluated_at="2026-08-28T20:10:00Z",
        expected_prior_snapshot_sha256="0" * 64,
    )
    assert result["verdict"] == "REFUSE"
    assert result["delete_through_generation"] == 0


def test_snapshot_chain_accepts_valid_anchor_progression() -> None:
    first = _snapshot(_prepared())
    second = _snapshot(_committed(), prior=first["snapshot_sha256"])
    result = audit_snapshot_chain([first, second])
    assert result["verdict"] == "PASS"
    assert result["highest_generation"] == 3


def test_snapshot_chain_refuses_stale_anchor_and_generation_rollback() -> None:
    first = _snapshot(_committed())
    second = _snapshot(_prepared(), prior=first["snapshot_sha256"])
    result = audit_snapshot_chain([first, second])
    assert result["verdict"] == "REFUSE"
    assert any("GENERATION_ROLLBACK" in error for error in result["errors"])
    second = _snapshot(_committed())
    assert any(
        "ANCHOR_INVALID" in error for error in audit_snapshot_chain([first, second])["errors"]
    )


def test_divergent_snapshots_at_same_generation_are_refused() -> None:
    first = _snapshot()
    divergent = copy.deepcopy(first)
    divergent["prior_snapshot_sha256"] = first["snapshot_sha256"]
    divergent["compaction_policy_version"] = POLICY_VERSION
    divergent["source_validation_sha256"] = "f" * 64
    _rehash(divergent)
    result = audit_snapshot_chain([first, divergent])
    assert result["verdict"] == "REFUSE"
    assert any("DIVERGENT_SAME_GENERATION" in error for error in result["errors"])


def test_source_hash_corruption_refuses_snapshot_and_compaction() -> None:
    records = _committed()
    snapshot = _snapshot(records)
    records[1]["record_sha256"] = "0" * 64
    source = _source(records)
    assert source["verdict"] == "REFUSE"
    assert (
        validate_snapshot(snapshot, source, expected_prior_snapshot_sha256="0" * 64)["verdict"]
        == "REFUSE"
    )


def test_snapshot_validation_has_no_write_or_order_capability() -> None:
    safety = _validate(_snapshot())["safety"]
    assert safety["read_only"] is True
    assert all(value is False for key, value in safety.items() if key != "read_only")
