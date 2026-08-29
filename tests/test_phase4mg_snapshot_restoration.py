from __future__ import annotations

import copy

import pytest

from scripts.local.phase4me_nonce_consumption_ledger import make_record
from scripts.local.phase4mg_snapshot_restoration import (
    _body_hash,
    artifact_errors,
    audit_migration_lineages,
    migrate_snapshot,
    restore_snapshot,
)
from tests.test_phase4me_nonce_consumption_ledger import (
    NONCE,
    RECEIPT,
    TOKEN,
    _committed,
    _prepared,
)
from tests.test_phase4mf_nonce_ledger_snapshot import _snapshot


def _artifact(records=None):
    return migrate_snapshot(_snapshot(records or _committed()), from_version=1, to_version=2)


def _restore(artifact, suffix=None, **kwargs):
    return restore_snapshot(
        artifact,
        suffix or [],
        evaluated_at="2026-08-28T20:10:00Z",
        expected_snapshot_sha256=kwargs.get(
            "expected_snapshot_sha256", artifact["source_snapshot_sha256"]
        ),
        expected_source_generation=kwargs.get(
            "expected_source_generation", artifact["source_generation"]
        ),
        expected_source_head_sha256=kwargs.get(
            "expected_source_head_sha256", artifact["source_head_sha256"]
        ),
        expected_prior_snapshot_sha256=kwargs.get(
            "expected_prior_snapshot_sha256", artifact["prior_snapshot_sha256"]
        ),
    )


def _rehash(artifact):
    artifact["artifact_sha256"] = _body_hash(artifact)


def _continuation(snapshot, operation, *, receipt=None):
    return make_record(
        operation,
        generation=snapshot["source_generation"] + 1,
        expected_generation=snapshot["source_generation"],
        transaction_id="tx-1",
        token_id=TOKEN,
        nonce_sha256=NONCE,
        receipt_sha256=receipt,
        occurred_at="2026-08-28T20:05:00Z",
        previous_record_sha256=snapshot["source_head_sha256"],
    )


def test_migration_and_empty_suffix_round_trip_are_deterministic() -> None:
    artifact = _artifact()
    assert artifact == _artifact()
    assert artifact_errors(artifact) == []
    assert migrate_snapshot(artifact, from_version=2, to_version=2) == artifact
    result = _restore(artifact)
    assert result["verdict"] == "PASS"
    assert result["generation"] == artifact["source_generation"]
    assert result["transactions"] == artifact["transactions"]
    assert result["reserved_or_consumed_nonce_sha256"] == [NONCE]


def test_prepared_snapshot_plus_suffix_restores_consumed_state() -> None:
    snapshot = _snapshot(_prepared())
    artifact = migrate_snapshot(snapshot, from_version=1, to_version=2)
    suffix = [_continuation(snapshot, "DURABLE_CONSUME", receipt=RECEIPT)]
    result = _restore(artifact, suffix)
    assert result["verdict"] == "PASS"
    assert result["transactions"]["tx-1"]["state"] == "CONSUMED"
    assert result["reserved_or_consumed_nonce_sha256"] == [NONCE]


@pytest.mark.parametrize("from_version,to_version", [(0, 2), (1, 3), (2, 1), (9, 9)])
def test_unknown_skipped_and_downgrade_migrations_are_refused(from_version, to_version) -> None:
    with pytest.raises(ValueError):
        migrate_snapshot(_snapshot(), from_version=from_version, to_version=to_version)


@pytest.mark.parametrize(
    "mutation,error",
    [
        (
            lambda row: row.update(reserved_or_consumed_nonce_sha256=[]),
            "NONCE_SET_LOSS_OR_EXPANSION",
        ),
        (
            lambda row: row["transactions"]["tx-1"].update(state="PREPARED"),
            "TRANSACTION_RECEIPT_INVALID",
        ),
        (lambda row: row.update(source_snapshot_sha256="f" * 64), None),
        (lambda row: row.update(source_head_sha256="f" * 64), None),
        (lambda row: row.update(prior_snapshot_sha256="f" * 64), None),
        (
            lambda row: row.update(restoration_policy_version="evil.v1"),
            "RESTORATION_POLICY_INVALID",
        ),
        (lambda row: row.update(migration_lineage=[]), "MIGRATION_LINEAGE_INVALID"),
    ],
)
def test_field_loss_state_widening_hash_and_policy_substitution_fail(mutation, error) -> None:
    artifact = _artifact()
    mutation(artifact)
    _rehash(artifact)
    errors = artifact_errors(artifact)
    if error:
        assert error in errors
    else:
        baseline = _artifact()
        result = _restore(
            artifact,
            expected_snapshot_sha256=baseline["source_snapshot_sha256"],
            expected_source_generation=baseline["source_generation"],
            expected_source_head_sha256=baseline["source_head_sha256"],
            expected_prior_snapshot_sha256=baseline["prior_snapshot_sha256"],
        )
        assert result["verdict"] == "REFUSE"


def test_snapshot_anchor_substitution_is_refused() -> None:
    artifact = _artifact()
    result = _restore(artifact, expected_snapshot_sha256="f" * 64)
    assert "SNAPSHOT_ANCHOR_SUBSTITUTION" in result["errors"]


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda row: row.update(expected_generation=0), "GENERATION_GAP_OR_ROLLBACK"),
        (lambda row: row.update(generation=9), "GENERATION_GAP_OR_ROLLBACK"),
        (lambda row: row.update(previous_record_sha256="f" * 64), "SUFFIX_ANCHOR_INCOMPATIBLE"),
        (lambda row: row.update(token_id="f" * 64), "TOKEN_OR_NONCE_SUBSTITUTION"),
        (lambda row: row.update(nonce_sha256="f" * 64), "TOKEN_OR_NONCE_SUBSTITUTION"),
    ],
)
def test_incompatible_suffix_gap_and_substitution_are_refused(mutation, error: str) -> None:
    snapshot = _snapshot(_prepared())
    artifact = migrate_snapshot(snapshot, from_version=1, to_version=2)
    record = _continuation(snapshot, "DURABLE_CONSUME", receipt=RECEIPT)
    mutation(record)
    record["record_sha256"] = (
        __import__("hashlib")
        .sha256(
            __import__("json")
            .dumps(
                {key: value for key, value in record.items() if key != "record_sha256"},
                sort_keys=True,
                separators=(",", ":"),
            )
            .encode()
        )
        .hexdigest()
    )
    result = _restore(artifact, [record])
    assert result["verdict"] == "REFUSE"
    assert any(error in value for value in result["errors"])


def test_duplicate_nonce_owner_in_suffix_is_refused() -> None:
    artifact = _artifact()
    snapshot = _snapshot()
    record = make_record(
        "PREPARE",
        generation=4,
        expected_generation=3,
        transaction_id="tx-2",
        token_id="d" * 64,
        nonce_sha256=NONCE,
        receipt_sha256=None,
        occurred_at="2026-08-28T20:05:00Z",
        previous_record_sha256=snapshot["source_head_sha256"],
    )
    assert any(
        "DUPLICATE_NONCE_OWNERSHIP" in value for value in _restore(artifact, [record])["errors"]
    )


def test_divergent_migration_paths_are_refused() -> None:
    first = _artifact()
    divergent = copy.deepcopy(first)
    divergent["prior_snapshot_sha256"] = "f" * 64
    _rehash(divergent)
    result = audit_migration_lineages([first, divergent])
    assert result["verdict"] == "REFUSE"
    assert any("DIVERGENT_MIGRATION_PATH" in error for error in result["errors"])


def test_restoration_has_no_write_runtime_or_order_capability() -> None:
    safety = _restore(_artifact())["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
