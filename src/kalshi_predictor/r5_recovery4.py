from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class R5CertificationLimits:
    runtime_seconds: float = 2700.0
    memory_peak_bytes: int = 2_200 * 1024 * 1024
    heartbeat_age_seconds: float = 30.0
    required_cycles: int = 3


FAILURE_ORDER = (
    "execution_enabled",
    "oom",
    "timeout",
    "memory_limit",
    "stale_heartbeat",
    "lock_contention",
    "output_mismatch",
    "missing_measurement",
)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def assess_cycle(
    cycle: dict[str, Any], limits: R5CertificationLimits
) -> dict[str, Any]:
    missing = sorted(
        field
        for field in (
            "cycle_id",
            "runtime_seconds",
            "memory_peak_bytes",
            "heartbeat_max_age_seconds",
            "output_parity",
            "writer_clear_after",
            "locks_clear_after",
            "execution_enabled",
            "oom_events",
        )
        if field not in cycle
    )
    failures: list[str] = []
    if missing:
        failures.append("missing_measurement")
    if cycle.get("execution_enabled") is True:
        failures.append("execution_enabled")
    if int(cycle.get("oom_events", 0) or 0) > 0 or int(
        cycle.get("oom_kill_events", 0) or 0
    ) > 0:
        failures.append("oom")
    if (
        cycle.get("timed_out") is True
        or float(cycle.get("runtime_seconds", 0) or 0) > limits.runtime_seconds
    ):
        failures.append("timeout")
    if int(cycle.get("memory_peak_bytes", 0) or 0) > limits.memory_peak_bytes:
        failures.append("memory_limit")
    if float(cycle.get("heartbeat_max_age_seconds", 0) or 0) > limits.heartbeat_age_seconds:
        failures.append("stale_heartbeat")
    if cycle.get("writer_clear_after") is False or cycle.get("locks_clear_after") is False:
        failures.append("lock_contention")
    if cycle.get("output_parity") is False:
        failures.append("output_mismatch")

    ordered = [name for name in FAILURE_ORDER if name in failures]
    return {
        "cycle_id": cycle.get("cycle_id"),
        "status": "PASSED" if not ordered else "FAILED",
        "failures": ordered,
        "missing_measurements": missing,
        "measurements": {
            key: cycle.get(key)
            for key in (
                "runtime_seconds",
                "memory_current_end_bytes",
                "memory_peak_bytes",
                "memory_high_events",
                "oom_events",
                "oom_kill_events",
                "heartbeat_max_age_seconds",
                "output_parity",
                "writer_clear_after",
                "locks_clear_after",
                "execution_enabled",
            )
        },
    }


def build_certification_report(
    *,
    baseline: dict[str, Any],
    cycles: Iterable[dict[str, Any]],
    limits: R5CertificationLimits | None = None,
) -> dict[str, Any]:
    limits = limits or R5CertificationLimits()
    cycle_rows = list(cycles)
    assessments = [assess_cycle(cycle, limits) for cycle in cycle_rows]
    distinct_ids = {row["cycle_id"] for row in assessments if row["cycle_id"] is not None}
    all_pass = bool(assessments) and all(row["status"] == "PASSED" for row in assessments)
    census_complete = len(distinct_ids) >= limits.required_cycles
    post_runtime = [
        float(row["runtime_seconds"])
        for row in cycle_rows
        if row.get("runtime_seconds") is not None
    ]
    post_memory = [
        int(row["memory_peak_bytes"])
        for row in cycle_rows
        if row.get("memory_peak_bytes") is not None
    ]
    baseline_runtime = baseline.get("runtime_seconds")
    baseline_memory = baseline.get("memory_peak_bytes")
    failures = sorted({failure for row in assessments for failure in row["failures"]})
    status = "PASSED" if all_pass and census_complete else "FAILED" if failures else "WAITING"
    report: dict[str, Any] = {
        "phase": "R5-RECOVERY-4",
        "mode": "LOCAL_READ_ONLY_HARNESS",
        "status": status,
        "execution_enabled": False,
        "cloud_access": False,
        "database_writes": 0,
        "service_changes": 0,
        "threshold_changes": 0,
        "limits": {
            "runtime_seconds": limits.runtime_seconds,
            "memory_peak_bytes": limits.memory_peak_bytes,
            "heartbeat_age_seconds": limits.heartbeat_age_seconds,
            "required_distinct_cycles": limits.required_cycles,
        },
        "baseline": baseline,
        "comparison": {
            "post_cycle_count": len(cycle_rows),
            "distinct_cycle_count": len(distinct_ids),
            "worst_post_runtime_seconds": max(post_runtime, default=None),
            "worst_post_memory_peak_bytes": max(post_memory, default=None),
            "runtime_improvement_percent": _improvement(
                baseline_runtime, max(post_runtime, default=None)
            ),
            "memory_improvement_percent": _improvement(
                baseline_memory, max(post_memory, default=None)
            ),
            "all_outputs_match": bool(cycle_rows)
            and all(cycle.get("output_parity") is True for cycle in cycle_rows),
            "all_heartbeats_fresh": bool(cycle_rows)
            and all(
                float(cycle.get("heartbeat_max_age_seconds", float("inf")))
                <= limits.heartbeat_age_seconds
                for cycle in cycle_rows
            ),
        },
        "cycle_assessments": assessments,
        "failure_summary": failures,
        "rollback_recommendation": _rollback_recommendation(failures, census_complete),
        "three_cycle_census": {
            "complete": census_complete,
            "required_distinct_cycles": limits.required_cycles,
            "observed_distinct_cycles": len(distinct_ids),
            "collection_rules": [
                "Run one bounded cycle at a time behind authoritative writer and lock clearance.",
                "Require a fresh heartbeat at least every 30 seconds during R3 and R5 stages.",
                "Capture runtime, MemoryCurrent, MemoryPeak, memory.events, output hashes, "
                "and post-cycle locks.",
                "Stop immediately on timeout, OOM, stale heartbeat, invalid output, "
                "contention, or execution enablement.",
                "Do not restart the legacy 32-cycle service during certification.",
            ],
        },
        "next_phase": (
            "R5-RECOVERY-5 Preview — Scheduler Re-entry and Overlap-Prevention "
            "Certification"
        ),
    }
    report["report_sha256"] = _sha256(report)
    return report


def _improvement(baseline: Any, post: Any) -> float | None:
    if baseline in (None, 0) or post is None:
        return None
    return round((float(baseline) - float(post)) / float(baseline) * 100.0, 3)


def _rollback_recommendation(failures: list[str], census_complete: bool) -> dict[str, Any]:
    if failures:
        return {
            "action": "ROLL_BACK_AND_HOLD",
            "reason": "One or more fail-closed certification gates failed.",
            "failed_gates": failures,
            "allow_legacy_32_cycle_restart": False,
        }
    if not census_complete:
        return {
            "action": "HOLD_FOR_MORE_CYCLES",
            "reason": "Fewer than three distinct passing cycles are available.",
            "failed_gates": [],
            "allow_legacy_32_cycle_restart": False,
        }
    return {
        "action": "ADVANCE_TO_R5_RECOVERY_5_PREVIEW",
        "reason": "Three distinct cycles passed every unchanged safety gate.",
        "failed_gates": [],
        "allow_legacy_32_cycle_restart": False,
    }


def write_report(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical_json(report), encoding="utf-8")
    return path
