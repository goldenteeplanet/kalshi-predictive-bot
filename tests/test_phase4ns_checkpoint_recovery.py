from __future__ import annotations

import copy

from scripts.local.phase4nr_long_horizon_soak import generate_workload, run_soak
from scripts.local.phase4ns_checkpoint_recovery import (
    build_fault_matrix,
    recover_newest_checkpoint,
    run_recovery_matrix,
)
from tests.test_phase4mz_adversarial_backtest import _records


def _workload():
    return generate_workload(seed=20260829, epochs=80, maximum_length=24, minimum_commands=700)


def _checkpoints(workload):
    return run_soak(
        workload,
        _records(),
        checkpoint_interval=20,
        maximum_seconds=120,
        maximum_bytes=10_000_000,
    )["checkpoints"]


def test_fault_matrix_exercises_all_declared_checkpoint_failures() -> None:
    workload = _workload()
    matrix = build_fault_matrix(_checkpoints(workload), workload)
    assert len(matrix) == 13
    assert {row["fault"] for row in matrix} == {
        "TRUNCATED",
        "BIT_FLIP",
        "STALE",
        "FUTURE_POSITION",
        "WRONG_WORKLOAD",
        "MISSING_FIELD",
        "DUPLICATE",
        "REORDERED_CHAIN",
        "CORRUPT_CUMULATIVE",
        "INTERRUPTED_CREATION",
        "PARTIAL_WRITE",
        "ROLLBACK_LAST_VALID",
        "SAME_EPOCH_DISAGREEMENT",
    }


def test_recovery_matrix_matches_expected_fail_closed_or_recovery_outcomes() -> None:
    workload = _workload()
    result = run_recovery_matrix(
        workload, _checkpoints(workload), _records(), maximum_rollback_epochs=20
    )
    assert result["verdict"] == "PASS"
    assert result["fault_count"] == 13


def test_newest_valid_is_selected_regardless_of_order_or_duplicates() -> None:
    workload = _workload()
    checkpoints = _checkpoints(workload)
    for candidates in (checkpoints, list(reversed(checkpoints)), checkpoints + [checkpoints[-1]]):
        result = recover_newest_checkpoint(
            workload, candidates, _records(), maximum_rollback_epochs=80
        )
        assert result["verdict"] == "PASS"
        assert result["selected_epoch"] == 80


def test_no_trustworthy_checkpoint_same_epoch_disagreement_and_rollback_bound_refuse() -> None:
    workload = _workload()
    checkpoints = _checkpoints(workload)
    faults = {row["fault"]: row for row in build_fault_matrix(checkpoints, workload)}
    none = recover_newest_checkpoint(
        workload, faults["TRUNCATED"]["candidates"], _records(), maximum_rollback_epochs=80
    )
    assert "NO_TRUSTWORTHY_CHECKPOINT" in none["errors"]
    disagreement = recover_newest_checkpoint(
        workload,
        faults["SAME_EPOCH_DISAGREEMENT"]["candidates"],
        _records(),
        maximum_rollback_epochs=80,
    )
    assert "CANDIDATE_DISAGREEMENT_AT_EPOCH" in disagreement["errors"]
    rollback = recover_newest_checkpoint(
        workload,
        faults["ROLLBACK_LAST_VALID"]["candidates"],
        _records(),
        maximum_rollback_epochs=10,
    )
    assert "ROLLBACK_BOUND_EXCEEDED" in rollback["errors"]


def test_recovery_matches_uninterrupted_final_coverage_refusals_and_provenance() -> None:
    workload = _workload()
    checkpoints = _checkpoints(workload)
    first = recover_newest_checkpoint(
        workload, checkpoints[:-1], _records(), maximum_rollback_epochs=80
    )
    second = recover_newest_checkpoint(
        workload, [checkpoints[-2]], _records(), maximum_rollback_epochs=80
    )
    for field in ("final_sha256", "coverage_sha256", "refusal_classes", "provenance_sha256"):
        assert first[field] == second[field]


def test_checkpoint_recovery_is_deterministic_and_input_preserving() -> None:
    workload = _workload()
    original = copy.deepcopy(workload)
    checkpoints = _checkpoints(workload)
    first = recover_newest_checkpoint(workload, checkpoints, _records(), maximum_rollback_epochs=80)
    assert first == recover_newest_checkpoint(
        workload, checkpoints, _records(), maximum_rollback_epochs=80
    )
    assert workload == original


def test_recovery_has_no_execution_capability() -> None:
    workload = _workload()
    safety = recover_newest_checkpoint(
        workload, _checkpoints(workload), _records(), maximum_rollback_epochs=80
    )["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
