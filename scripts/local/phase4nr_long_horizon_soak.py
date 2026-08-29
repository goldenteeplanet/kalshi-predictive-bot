"""Bounded long-horizon sequence soak with signed checkpoint resume proof."""

from __future__ import annotations

import hashlib
import json
import time

from scripts.local.phase4no_stateful_sequence_fuzz import execute_sequence, generate_sequences
from scripts.local.phase4np_transition_coverage import coverage_of

SCHEMA = "phase4nr.long-horizon-soak.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def generate_workload(
    *, seed: int, epochs: int, maximum_length: int, minimum_commands: int
) -> dict[str, object]:
    generated = generate_sequences(seed=seed, count=epochs, maximum_length=maximum_length)
    if generated["verdict"] != "PASS":
        return {"verdict": "REFUSE", "errors": generated["errors"], "epochs": []}
    rows = list(generated["sequences"])
    rotation = seed % len(rows)
    rows = rows[rotation:] + rows[:rotation]
    epochs_out = []
    for epoch, row in enumerate(rows):
        body = {
            "epoch": epoch,
            "source_ordinal": row["ordinal"],
            "seed": seed,
            "commands": row["commands"],
            "source_sequence_sha256": row["sequence_sha256"],
        }
        epochs_out.append({**body, "epoch_sha256": _digest(body)})
    command_count = sum(len(row["commands"]) for row in epochs_out)
    errors = []
    if command_count < minimum_commands:
        errors.append("FALSE_COMPLETION_COMMAND_FLOOR")
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "seed": seed,
        "epoch_count": len(epochs_out),
        "command_count": command_count,
        "minimum_commands": minimum_commands,
        "rotation": rotation,
        "epochs": epochs_out,
        "safety": _safety(),
    }
    result["workload_sha256"] = _digest(result)
    return result


def _summary(epoch: dict[str, object], execution: dict[str, object]) -> dict[str, object]:
    trace = [
        {
            key: event[key]
            for key in (
                "index",
                "command",
                "state_before",
                "state_after",
                "accepted",
                "refusal_code",
            )
        }
        for event in execution["events"]
    ]
    return {
        "epoch": epoch["epoch"],
        "epoch_sha256": epoch["epoch_sha256"],
        "terminal_state": execution["terminal_state"],
        "refusal_codes": execution["refusal_codes"],
        "trace_sha256": _digest(trace),
        "provenance": execution["provenance"],
        "execution_sha256": execution["execution_sha256"],
    }


def _run_epochs(epochs, records, *, initial_hash: str):
    cumulative = initial_hash
    summaries, executions = [], []
    for epoch in epochs:
        sequence = {
            "seed": epoch["seed"],
            "ordinal": epoch["source_ordinal"],
            "commands": epoch["commands"],
            "sequence_sha256": epoch["source_sequence_sha256"],
        }
        execution = execute_sequence(sequence, records)
        summary = _summary(epoch, execution)
        cumulative = _digest({"previous": cumulative, "summary": summary})
        summary["cumulative_sha256"] = cumulative
        summaries.append(summary)
        executions.append(execution)
    return summaries, executions, cumulative


def _checkpoint(workload_hash, next_epoch, cumulative_hash):
    body = {
        "schema": SCHEMA,
        "workload_sha256": workload_hash,
        "next_epoch": next_epoch,
        "cumulative_sha256": cumulative_hash,
    }
    return {**body, "checkpoint_sha256": _digest(body)}


def resume_from_checkpoint(
    workload: dict[str, object],
    checkpoint: dict[str, object],
    records: list[dict[str, object]],
) -> dict[str, object]:
    errors = []
    unsigned = {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
    if checkpoint.get("checkpoint_sha256") != _digest(unsigned):
        errors.append("CHECKPOINT_CORRUPTION")
    if checkpoint.get("workload_sha256") != workload.get("workload_sha256"):
        errors.append("CHECKPOINT_WORKLOAD_MISMATCH")
    next_epoch = checkpoint.get("next_epoch")
    if not isinstance(next_epoch, int) or not 0 <= next_epoch <= len(workload.get("epochs", [])):
        errors.append("CHECKPOINT_POSITION_INVALID")
    if errors:
        return {"verdict": "REFUSE", "errors": sorted(set(errors)), "safety": _safety()}
    suffix, executions, final_hash = _run_epochs(
        workload["epochs"][next_epoch:],
        records,
        initial_hash=checkpoint["cumulative_sha256"],
    )
    return {
        "verdict": "PASS",
        "errors": [],
        "next_epoch": next_epoch,
        "suffix": suffix,
        "executions": executions,
        "final_sha256": final_hash,
        "safety": _safety(),
    }


def run_soak(
    workload: dict[str, object],
    records: list[dict[str, object]],
    *,
    checkpoint_interval: int,
    maximum_seconds: float,
    maximum_bytes: int,
) -> dict[str, object]:
    started = time.perf_counter()
    errors = []
    epochs = workload.get("epochs", []) if isinstance(workload, dict) else []
    if (
        workload.get("verdict") != "PASS"
        or checkpoint_interval < 1
        or maximum_seconds <= 0
        or maximum_bytes < 1
    ):
        return _result(["SOAK_BOUND_OR_WORKLOAD_INVALID"], workload, [], [], {}, None, 0, 0)
    if [row.get("epoch") for row in epochs] != list(range(len(epochs))):
        errors.append("EPOCH_ORDER_DRIFT")
    if len({row.get("epoch_sha256") for row in epochs}) != len(epochs):
        errors.append("DUPLICATE_EPOCH")
    first, executions, final_hash = _run_epochs(epochs, records, initial_hash="0" * 64)
    second, _, second_hash = _run_epochs(epochs, records, initial_hash="0" * 64)
    if first != second or final_hash != second_hash:
        errors.append("NONDETERMINISTIC_OUTPUT")
    checkpoints = []
    for next_epoch in range(checkpoint_interval, len(epochs) + 1, checkpoint_interval):
        checkpoints.append(
            _checkpoint(
                workload["workload_sha256"], next_epoch, first[next_epoch - 1]["cumulative_sha256"]
            )
        )
    for checkpoint in checkpoints:
        resumed = resume_from_checkpoint(workload, checkpoint, records)
        expected_suffix = first[checkpoint["next_epoch"] :]
        if resumed.get("suffix") != expected_suffix or resumed.get("final_sha256") != final_hash:
            errors.append("RESUME_DIVERGENCE")
    coverage = coverage_of(executions)
    if any(row["verdict"] != "PASS" for row in executions):
        errors.append("STATE_MACHINE_INVARIANT_FAILED")
    if coverage["unsafe_accepted"]:
        errors.append("UNSAFE_STATE_ACCEPTED")
    completed_epochs = [row["epoch"] for row in first]
    if completed_epochs != list(range(len(epochs))):
        errors.append("SKIPPED_OR_DUPLICATED_COMMAND_EPOCH")
    encoded_bytes = len(json.dumps(first, sort_keys=True, separators=(",", ":")).encode())
    elapsed = time.perf_counter() - started
    if encoded_bytes > maximum_bytes or elapsed > maximum_seconds:
        errors.append("RESOURCE_BOUND_VIOLATION")
    return _result(
        errors, workload, first, checkpoints, coverage, final_hash, encoded_bytes, elapsed
    )


def _result(errors, workload, summaries, checkpoints, coverage, final_hash, encoded_bytes, elapsed):
    attested = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "workload_sha256": workload.get("workload_sha256") if isinstance(workload, dict) else None,
        "epoch_count": len(summaries),
        "command_count": workload.get("command_count", 0) if isinstance(workload, dict) else 0,
        "checkpoint_count": len(checkpoints),
        "checkpoints": checkpoints,
        "coverage": coverage,
        "final_sha256": final_hash,
        "encoded_trace_bytes": encoded_bytes,
        "safety": _safety(),
    }
    attested["proof_sha256"] = _digest(attested)
    attested["runtime_observation_seconds"] = elapsed
    return attested


def _safety():
    return {
        "offline_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
