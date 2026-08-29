from __future__ import annotations

import copy

from scripts.local.phase4nr_long_horizon_soak import (
    generate_workload,
    resume_from_checkpoint,
    run_soak,
)
from tests.test_phase4mz_adversarial_backtest import _records


def _workload(epochs=160, minimum_commands=1500):
    return generate_workload(
        seed=20260829, epochs=epochs, maximum_length=24, minimum_commands=minimum_commands
    )


def _soak(workload=None, **kwargs):
    return run_soak(
        workload or _workload(),
        _records(),
        checkpoint_interval=40,
        maximum_seconds=120,
        maximum_bytes=20_000_000,
        **kwargs,
    )


def test_workload_is_deterministic_rotated_and_contains_thousands_of_commands() -> None:
    first = _workload()
    assert first == _workload()
    assert first["verdict"] == "PASS"
    assert first["epoch_count"] == 160
    assert first["command_count"] >= 1500
    assert first["rotation"] != 0


def test_uninterrupted_and_every_checkpoint_resume_are_identical() -> None:
    result = _soak()
    assert result["verdict"] == "PASS"
    assert result["checkpoint_count"] == 4
    assert result["epoch_count"] == 160
    assert result["final_sha256"]


def test_checkpoint_corruption_position_and_workload_mismatch_refuse() -> None:
    workload = _workload()
    result = _soak(workload)
    checkpoint = result["checkpoints"][0]
    corrupted = copy.deepcopy(checkpoint)
    corrupted["cumulative_sha256"] = "f" * 64
    assert (
        "CHECKPOINT_CORRUPTION" in resume_from_checkpoint(workload, corrupted, _records())["errors"]
    )
    position = copy.deepcopy(checkpoint)
    position["next_epoch"] = 9999
    assert (
        "CHECKPOINT_POSITION_INVALID"
        in resume_from_checkpoint(workload, position, _records())["errors"]
    )
    other = copy.deepcopy(workload)
    other["workload_sha256"] = "0" * 64
    assert (
        "CHECKPOINT_WORKLOAD_MISMATCH"
        in resume_from_checkpoint(other, checkpoint, _records())["errors"]
    )


def test_epoch_order_duplicate_and_false_completion_fail_closed() -> None:
    order = _workload()
    order["epochs"][0]["epoch"] = 1
    assert "EPOCH_ORDER_DRIFT" in _soak(order)["errors"]
    duplicate = _workload()
    duplicate["epochs"][1]["epoch_sha256"] = duplicate["epochs"][0]["epoch_sha256"]
    assert "DUPLICATE_EPOCH" in _soak(duplicate)["errors"]
    short = _workload(epochs=2, minimum_commands=1000)
    assert "FALSE_COMPLETION_COMMAND_FLOOR" in short["errors"]


def test_runtime_memory_and_invalid_bounds_fail_closed() -> None:
    result = _soak()
    assert result["runtime_observation_seconds"] <= 120
    assert result["encoded_trace_bytes"] <= 20_000_000
    tiny = run_soak(
        _workload(), _records(), checkpoint_interval=40, maximum_seconds=120, maximum_bytes=1
    )
    assert "RESOURCE_BOUND_VIOLATION" in tiny["errors"]
    invalid = run_soak(
        _workload(), _records(), checkpoint_interval=0, maximum_seconds=120, maximum_bytes=100
    )
    assert "SOAK_BOUND_OR_WORKLOAD_INVALID" in invalid["errors"]


def test_soak_proof_is_attested_independently_of_runtime_observation() -> None:
    first, second = _soak(), _soak()
    assert first["proof_sha256"] == second["proof_sha256"]
    assert first["final_sha256"] == second["final_sha256"]
    assert first["coverage"] == second["coverage"]


def test_long_horizon_soak_has_no_execution_capability() -> None:
    safety = _soak()["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
