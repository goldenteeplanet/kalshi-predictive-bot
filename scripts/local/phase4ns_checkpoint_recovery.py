"""Fault-injected checkpoint recovery matrix for the long-horizon soak."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4np_transition_coverage import coverage_of
from scripts.local.phase4nr_long_horizon_soak import (
    _run_epochs,
    resume_from_checkpoint,
)

SCHEMA = "phase4ns.checkpoint-recovery.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _sign(body: dict[str, object]) -> dict[str, object]:
    return {**body, "checkpoint_sha256": _digest(body)}


def _baseline(workload, records):
    summaries, executions, final_hash = _run_epochs(
        workload["epochs"], records, initial_hash="0" * 64
    )
    prefix = {0: "0" * 64}
    prefix.update({row["epoch"] + 1: row["cumulative_sha256"] for row in summaries})
    return {
        "summaries": summaries,
        "executions": executions,
        "final_hash": final_hash,
        "prefix": prefix,
        "coverage": coverage_of(executions),
        "refusal_classes": sorted(
            {code for execution in executions for code in execution["refusal_codes"]}
        ),
        "provenance_sha256": _digest([row["provenance"] for row in summaries]),
    }


def _recover(workload, candidates, records, maximum_rollback_epochs, baseline):
    errors, rejected = [], []
    signed_by_epoch: dict[int, set[str]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        unsigned = {key: value for key, value in candidate.items() if key != "checkpoint_sha256"}
        if candidate.get("checkpoint_sha256") == _digest(unsigned):
            epoch = candidate.get("next_epoch")
            cumulative = candidate.get("cumulative_sha256")
            if isinstance(epoch, int) and isinstance(cumulative, str):
                signed_by_epoch.setdefault(epoch, set()).add(cumulative)
    if any(len(values) > 1 for values in signed_by_epoch.values()):
        return _result(["CANDIDATE_DISAGREEMENT_AT_EPOCH"], None, rejected, baseline)

    valid = {}
    required = {"schema", "workload_sha256", "next_epoch", "cumulative_sha256", "checkpoint_sha256"}
    for ordinal, candidate in enumerate(candidates):
        reasons = []
        if not isinstance(candidate, dict) or set(candidate) != required:
            reasons.append("CHECKPOINT_SHAPE_INVALID")
        else:
            unsigned = {
                key: value for key, value in candidate.items() if key != "checkpoint_sha256"
            }
            if candidate["checkpoint_sha256"] != _digest(unsigned):
                reasons.append("CHECKPOINT_SIGNATURE_INVALID")
            if candidate["workload_sha256"] != workload["workload_sha256"]:
                reasons.append("CHECKPOINT_WORKLOAD_MISMATCH")
            position = candidate["next_epoch"]
            if not isinstance(position, int) or position not in baseline["prefix"]:
                reasons.append("CHECKPOINT_POSITION_INVALID")
            elif candidate["cumulative_sha256"] != baseline["prefix"][position]:
                reasons.append("CHECKPOINT_CUMULATIVE_MISMATCH")
        if reasons:
            rejected.append({"ordinal": ordinal, "reasons": sorted(set(reasons))})
        else:
            valid[candidate["next_epoch"]] = candidate
    if not valid:
        return _result(["NO_TRUSTWORTHY_CHECKPOINT"], None, rejected, baseline)
    selected_epoch = max(valid)
    newest_claimed = max(
        (
            candidate.get("next_epoch", -1)
            for candidate in candidates
            if isinstance(candidate, dict) and isinstance(candidate.get("next_epoch"), int)
        ),
        default=selected_epoch,
    )
    if newest_claimed - selected_epoch > maximum_rollback_epochs:
        return _result(["ROLLBACK_BOUND_EXCEEDED"], None, rejected, baseline)
    selected = valid[selected_epoch]
    resumed = resume_from_checkpoint(workload, selected, records)
    expected_suffix = baseline["summaries"][selected_epoch:]
    if (
        resumed.get("suffix") != expected_suffix
        or resumed.get("final_sha256") != baseline["final_hash"]
    ):
        errors.append("RECOVERY_DIVERGENCE")
    return _result(errors, selected, rejected, baseline)


def recover_newest_checkpoint(
    workload: dict[str, object],
    candidates: list[object],
    records: list[dict[str, object]],
    *,
    maximum_rollback_epochs: int,
) -> dict[str, object]:
    return _recover(
        workload,
        candidates,
        records,
        maximum_rollback_epochs,
        _baseline(workload, records),
    )


def build_fault_matrix(checkpoints: list[dict[str, object]], workload: dict[str, object]):
    oldest, newest = checkpoints[0], checkpoints[-1]
    previous = checkpoints[-2]
    truncated = {key: value for key, value in newest.items() if key != "checkpoint_sha256"}
    bit_flip = copy.deepcopy(newest)
    bit_flip["checkpoint_sha256"] = "0" + bit_flip["checkpoint_sha256"][1:]
    future_body = {
        **{key: value for key, value in newest.items() if key != "checkpoint_sha256"},
        "next_epoch": len(workload["epochs"]) + 1,
    }
    wrong_body = {
        **{key: value for key, value in newest.items() if key != "checkpoint_sha256"},
        "workload_sha256": "f" * 64,
    }
    missing = copy.deepcopy(newest)
    del missing["cumulative_sha256"]
    corrupt_body = {
        **{key: value for key, value in newest.items() if key != "checkpoint_sha256"},
        "cumulative_sha256": "a" * 64,
    }
    disagree_body = {
        **{key: value for key, value in newest.items() if key != "checkpoint_sha256"},
        "cumulative_sha256": "b" * 64,
    }
    return [
        {"fault": "TRUNCATED", "candidates": [truncated], "expected": "REFUSE"},
        {"fault": "BIT_FLIP", "candidates": [bit_flip], "expected": "REFUSE"},
        {"fault": "STALE", "candidates": [oldest], "expected": "PASS"},
        {
            "fault": "FUTURE_POSITION",
            "candidates": [future_body | {"checkpoint_sha256": _digest(future_body)}],
            "expected": "REFUSE",
        },
        {"fault": "WRONG_WORKLOAD", "candidates": [_sign(wrong_body)], "expected": "REFUSE"},
        {"fault": "MISSING_FIELD", "candidates": [missing], "expected": "REFUSE"},
        {"fault": "DUPLICATE", "candidates": [newest, copy.deepcopy(newest)], "expected": "PASS"},
        {"fault": "REORDERED_CHAIN", "candidates": list(reversed(checkpoints)), "expected": "PASS"},
        {"fault": "CORRUPT_CUMULATIVE", "candidates": [_sign(corrupt_body)], "expected": "REFUSE"},
        {"fault": "INTERRUPTED_CREATION", "candidates": [{"schema": SCHEMA}], "expected": "REFUSE"},
        {"fault": "PARTIAL_WRITE", "candidates": [b'{"schema":'], "expected": "REFUSE"},
        {
            "fault": "ROLLBACK_LAST_VALID",
            "candidates": [previous, _sign(corrupt_body)],
            "expected": "PASS",
        },
        {
            "fault": "SAME_EPOCH_DISAGREEMENT",
            "candidates": [newest, _sign(disagree_body)],
            "expected": "REFUSE",
        },
    ]


def run_recovery_matrix(
    workload: dict[str, object],
    checkpoints: list[dict[str, object]],
    records: list[dict[str, object]],
    *,
    maximum_rollback_epochs: int,
) -> dict[str, object]:
    baseline = _baseline(workload, records)
    cases = []
    errors = []
    for specification in build_fault_matrix(checkpoints, workload):
        recovery = _recover(
            workload,
            specification["candidates"],
            records,
            maximum_rollback_epochs,
            baseline,
        )
        observed = recovery["verdict"]
        if observed != specification["expected"]:
            errors.append("FAULT_EXPECTATION_MISMATCH")
        cases.append(
            {
                "fault": specification["fault"],
                "expected": specification["expected"],
                "observed": observed,
                "selected_epoch": recovery.get("selected_epoch"),
                "errors": recovery["errors"],
                "recovery_sha256": recovery["recovery_sha256"],
            }
        )
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "fault_count": len(cases),
        "cases": cases,
        "baseline_final_sha256": baseline["final_hash"],
        "safety": _safety(),
    }
    result["matrix_sha256"] = _digest(result)
    return result


def _result(errors, selected, rejected, baseline):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "selected_epoch": selected.get("next_epoch") if selected else None,
        "selected_checkpoint_sha256": selected.get("checkpoint_sha256") if selected else None,
        "rejected": rejected,
        "final_sha256": baseline["final_hash"] if not errors else None,
        "coverage_sha256": baseline["coverage"]["coverage_sha256"] if not errors else None,
        "refusal_classes": baseline["refusal_classes"] if not errors else None,
        "provenance_sha256": baseline["provenance_sha256"] if not errors else None,
        "safety": _safety(),
    }
    result["recovery_sha256"] = _digest(result)
    return result


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
