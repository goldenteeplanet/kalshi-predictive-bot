"""R5-RECOVERY-8 read-only three-cycle stability consolidation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from statistics import mean
from typing import Any


def consolidate_three_cycle_census(census: dict[str, Any]) -> dict[str, Any]:
    """Consolidate an R5-RECOVERY-6/6A report without runtime access."""
    assessments = census.get("cycle_assessments")
    if not isinstance(assessments, list):
        raise ValueError("cycle_assessments must be an array")
    passing = [row for row in assessments if row.get("status") == "PASSED"]
    distinct_ids = [str(row.get("cycle_id")) for row in passing]
    evidence = [row.get("evidence", {}) for row in passing]
    complete = len(passing) == 3 and len(set(distinct_ids)) == 3

    runtimes = _numbers(evidence, "runtime_seconds")
    memory_peaks = _numbers(evidence, "memory_peak_bytes")
    r3_ages = _numbers(evidence, "r3_heartbeat_max_age_seconds")
    r5_ages = _numbers(evidence, "r5_heartbeat_max_age_seconds")
    hashes = {str(row.get("actual_output_sha256")) for row in evidence}
    row_counts = {row.get("actual_row_count") for row in evidence}
    no_oom = all(
        int(row.get("memory_events", {}).get("oom", 0)) == 0
        and int(row.get("memory_events", {}).get("oom_kill", 0)) == 0
        for row in evidence
    )
    gates = {
        "all_cycles_passed": complete,
        "execution_disabled": complete
        and all(row.get("execution_enabled") is False for row in evidence),
        "heartbeat_fresh": complete and max(r3_ages + r5_ages, default=float("inf")) <= 30,
        "no_oom": complete and no_oom,
        "output_hash_parity": complete and len(hashes) == 1 and "None" not in hashes,
        "row_count_parity": complete and len(row_counts) == 1 and None not in row_counts,
        "writer_and_locks_clear": complete
        and all(
            row.get("writer_clear_after") is True and row.get("locks_clear_after") is True
            for row in evidence
        ),
    }
    report: dict[str, Any] = {
        "phase": "R5-RECOVERY-8",
        "mode": "READ_ONLY_THREE_CYCLE_CONSOLIDATION",
        "status": "PASSED" if all(gates.values()) else "FAILED",
        "cycle_ids": distinct_ids,
        "metrics": {
            "runtime_seconds": _summary(runtimes),
            "memory_peak_bytes": _summary(memory_peaks),
            "r3_heartbeat_max_age_seconds": _summary(r3_ages),
            "r5_heartbeat_max_age_seconds": _summary(r5_ages),
            "output_sha256": next(iter(hashes)) if len(hashes) == 1 else None,
            "row_count": next(iter(row_counts)) if len(row_counts) == 1 else None,
        },
        "gates": gates,
        "source_report_sha256": census.get("report_sha256"),
        "decision": (
            "ELIGIBLE_FOR_R5_RECOVERY_9_APPROVAL_REVIEW"
            if all(gates.values())
            else "DO_NOT_ADVANCE"
        ),
        "guardrails": {
            "cloud_access": False,
            "database_writes": 0,
            "service_changes": 0,
            "execution_enabled": False,
            "threshold_changes": 0,
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical(report).encode()).hexdigest()
    return report


def write_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "r5_recovery8_three_cycle_stability_census.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(_canonical(report), encoding="utf-8")
    temporary.replace(path)
    return path


def _numbers(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [float(row[key]) for row in rows if row.get(key) is not None]


def _summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "max": None, "mean": None, "delta_first_to_last": None}
    return {
        "min": min(values),
        "max": max(values),
        "mean": round(mean(values), 6),
        "delta_first_to_last": values[-1] - values[0],
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
