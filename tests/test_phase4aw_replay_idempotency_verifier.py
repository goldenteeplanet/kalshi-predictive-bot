from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4aw_replay_idempotency_verifier.py"
    spec = importlib.util.spec_from_file_location("phase4aw_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _event(sequence: int, **overrides):
    event = {
        "sequence": sequence,
        "attempt_id": f"attempt-{sequence}",
        "operation_hash": "a" * 64,
        "ticker": "KXAW-1",
        "envelope_hash": "b" * 64,
        "envelope_generation": 1,
        "approval_hash": "c" * 64,
        "database_state_before_hash": "d" * 64,
        "database_state_after_hash": "d" * 64,
        "outcome": "ROLLED_BACK",
        "receipt_hash": None,
    }
    event.update(overrides)
    return event


def _write(tmp_path: Path, events: list[dict]):
    module = _module()
    payload = {"schema": module.HISTORY_SCHEMA, "events": events, "execution_authorized": False}
    payload["artifact_hash"] = module._hash(payload)
    path = tmp_path / "history.json"
    path.write_text(json.dumps(payload))
    return module, path


def _reasons(verdict):
    return {reason for row in verdict["rows"] for reason in row["reason_codes"]}


def test_rollback_then_new_attempt_success_allows_only_first_mutation(tmp_path: Path):
    events = [
        _event(1),
        _event(
            2,
            attempt_id="attempt-2",
            outcome="SUCCESS",
            database_state_after_hash="e" * 64,
            receipt_hash="f" * 64,
        ),
    ]
    module, path = _write(tmp_path, events)
    verdict, proof = module.build(path, now=NOW)
    assert [row["may_mutate"] for row in verdict["rows"]] == [False, True]
    assert verdict["no_replay_can_cause_second_mutation"] is True
    assert proof["second_mutation_count"] == 0
    assert verdict["artifact_hash"] == module._hash(verdict)


def test_exact_replay_and_replay_after_success_are_refused(tmp_path: Path):
    events = [
        _event(1, outcome="SUCCESS", database_state_after_hash="e" * 64, receipt_hash="f" * 64),
        _event(
            2,
            attempt_id="attempt-1",
            outcome="SUCCESS",
            database_state_before_hash="e" * 64,
            database_state_after_hash="1" * 64,
            receipt_hash="2" * 64,
        ),
    ]
    module, path = _write(tmp_path, events)
    verdict, _ = module.build(path, now=NOW)
    assert {"DUPLICATE_ATTEMPT_ID", "OPERATION_ALREADY_TERMINAL"} <= _reasons(verdict)
    assert sum(row["may_mutate"] for row in verdict["rows"]) == 1


@pytest.mark.parametrize("outcome", ["TIMEOUT", "ROLLED_BACK"])
def test_replay_after_nonmutation_needs_new_attempt_id(tmp_path: Path, outcome: str):
    events = [_event(1, outcome=outcome), _event(2, attempt_id="attempt-1", outcome=outcome)]
    module, path = _write(tmp_path, events)
    verdict, _ = module.build(path, now=NOW)
    assert verdict["rows"][1]["decision"] == "REFUSED"
    assert "DUPLICATE_ATTEMPT_ID" in verdict["rows"][1]["reason_codes"]


def test_ambiguous_attempt_is_terminal_even_without_receipt(tmp_path: Path):
    events = [
        _event(1, outcome="AMBIGUOUS"),
        _event(
            2,
            attempt_id="attempt-2",
            outcome="SUCCESS",
            database_state_after_hash="e" * 64,
            receipt_hash="f" * 64,
        ),
    ]
    module, path = _write(tmp_path, events)
    verdict, _ = module.build(path, now=NOW)
    assert "OPERATION_ALREADY_TERMINAL" in verdict["rows"][1]["reason_codes"]
    assert not any(row["may_mutate"] for row in verdict["rows"])


def test_receipt_reuse_partial_loss_and_state_change_fail_closed(tmp_path: Path):
    events = [
        _event(
            1,
            operation_hash="1" * 64,
            outcome="SUCCESS",
            database_state_after_hash="e" * 64,
            receipt_hash="f" * 64,
        ),
        _event(
            2, operation_hash="2" * 64, database_state_before_hash="e" * 64, receipt_hash="f" * 64
        ),
        _event(
            3,
            operation_hash="3" * 64,
            outcome="SUCCESS",
            database_state_before_hash="e" * 64,
            database_state_after_hash="9" * 64,
        ),
    ]
    module, path = _write(tmp_path, events)
    verdict, _ = module.build(path, now=NOW)
    assert {"RECEIPT_REUSE", "SUCCESS_RECEIPT_MISSING"} <= _reasons(verdict)


def test_superseded_envelope_changed_approval_and_database_discontinuity_refuse(tmp_path: Path):
    events = [
        _event(1, envelope_generation=2, envelope_hash="2" * 64),
        _event(
            2,
            operation_hash="2" * 64,
            envelope_generation=1,
            approval_hash="9" * 64,
            database_state_before_hash="8" * 64,
            database_state_after_hash="8" * 64,
        ),
    ]
    module, path = _write(tmp_path, events)
    verdict, _ = module.build(path, now=NOW)
    assert {"SUPERSEDED_ENVELOPE", "DATABASE_STATE_DISCONTINUITY"} <= _reasons(verdict)


def test_nonmutating_outcome_with_changed_state_and_success_without_change_refuse(tmp_path: Path):
    events = [
        _event(1, database_state_after_hash="e" * 64),
        _event(
            2,
            operation_hash="2" * 64,
            outcome="SUCCESS",
            receipt_hash="f" * 64,
        ),
    ]
    module, path = _write(tmp_path, events)
    verdict, _ = module.build(path, now=NOW)
    assert {"NONMUTATING_OUTCOME_CHANGED_STATE", "SUCCESS_WITHOUT_STATE_CHANGE"} <= _reasons(
        verdict
    )


def test_tampering_sequence_missing_events_and_naive_time_fail_closed(tmp_path: Path):
    module, path = _write(tmp_path, [_event(1)])
    payload = json.loads(path.read_text())
    payload["events"][0]["attempt_id"] = "tampered"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH_INVALID"):
        module.build(path, now=NOW)
    module, path = _write(tmp_path, [_event(2)])
    with pytest.raises(ValueError, match="SEQUENCE_INVALID"):
        module.build(path, now=NOW)
    module, path = _write(tmp_path, [])
    with pytest.raises(ValueError, match="EVENTS_MISSING"):
        module.build(path, now=NOW)
    module, path = _write(tmp_path, [_event(1)])
    with pytest.raises(ValueError, match="TIMEZONE_MISSING"):
        module.build(path, now=datetime(2026, 8, 25, 12, 0))


def test_static_verifier_has_no_mutation_or_runtime_capability():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4aw_replay_idempotency_verifier.py"
    ).read_text()
    assert "sqlite3" not in source
    assert "--production-db" not in source
    assert "systemctl" not in source
