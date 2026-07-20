from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvidenceLimits:
    runtime_seconds: float = 2700.0
    memory_peak_bytes: int = 2_200 * 1024 * 1024
    heartbeat_age_seconds: float = 30.0
    required_cycles: int = 3


FAILURE_ORDER = (
    "execution_enabled",
    "timeout",
    "oom",
    "memory_limit",
    "stale_heartbeat",
    "output_mismatch",
    "row_count_mismatch",
    "writer_contention",
    "lock_contention",
    "service_failure",
    "incomplete_evidence",
)


def parse_systemd_properties(text: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key:
            properties[key.strip()] = value.strip()
    return properties


def normalize_cycle_evidence(raw: dict[str, Any]) -> dict[str, Any]:
    systemd = raw.get("systemd", {})
    if isinstance(systemd, str):
        systemd = parse_systemd_properties(systemd)
    memory_events = raw.get("memory_events", {})
    if isinstance(memory_events, str):
        memory_events = {
            key: int(value)
            for line in memory_events.splitlines()
            if len(parts := line.split()) == 2
            for key, value in [parts]
        }
    return {
        "cycle_id": raw.get("cycle_id"),
        "started_at": raw.get("started_at"),
        "ended_at": raw.get("ended_at"),
        "result": systemd.get("Result", raw.get("result")),
        "exec_main_status": _integer(systemd.get("ExecMainStatus", raw.get("exec_main_status"))),
        "runtime_seconds": _number(raw.get("runtime_seconds")),
        "memory_current_bytes": _integer(
            systemd.get("MemoryCurrent", raw.get("memory_current_bytes"))
        ),
        "memory_peak_bytes": _integer(
            systemd.get("MemoryPeak", raw.get("memory_peak_bytes"))
        ),
        "memory_events": memory_events,
        "r3_heartbeat_max_age_seconds": _number(raw.get("r3_heartbeat_max_age_seconds")),
        "r5_heartbeat_max_age_seconds": _number(raw.get("r5_heartbeat_max_age_seconds")),
        "expected_output_sha256": raw.get("expected_output_sha256"),
        "actual_output_sha256": raw.get("actual_output_sha256"),
        "expected_row_count": _integer(raw.get("expected_row_count")),
        "actual_row_count": _integer(raw.get("actual_row_count")),
        "writer_clear_after": raw.get("writer_clear_after"),
        "locks_clear_after": raw.get("locks_clear_after"),
        "execution_enabled": raw.get("execution_enabled"),
    }


def assess_cycle(raw: dict[str, Any], limits: EvidenceLimits | None = None) -> dict[str, Any]:
    limits = limits or EvidenceLimits()
    cycle = normalize_cycle_evidence(raw)
    required = (
        "cycle_id",
        "started_at",
        "ended_at",
        "result",
        "exec_main_status",
        "runtime_seconds",
        "memory_peak_bytes",
        "r3_heartbeat_max_age_seconds",
        "r5_heartbeat_max_age_seconds",
        "expected_output_sha256",
        "actual_output_sha256",
        "expected_row_count",
        "actual_row_count",
        "writer_clear_after",
        "locks_clear_after",
        "execution_enabled",
    )
    failures: list[str] = []
    missing = sorted(key for key in required if cycle.get(key) is None)
    if cycle.get("execution_enabled") is True:
        failures.append("execution_enabled")
    if (
        cycle.get("runtime_seconds") is not None
        and cycle["runtime_seconds"] > limits.runtime_seconds
    ):
        failures.append("timeout")
    memory_events = cycle["memory_events"]
    if int(memory_events.get("oom", 0)) > 0 or int(memory_events.get("oom_kill", 0)) > 0:
        failures.append("oom")
    if (
        cycle.get("memory_peak_bytes") is not None
        and cycle["memory_peak_bytes"] > limits.memory_peak_bytes
    ):
        failures.append("memory_limit")
    heartbeat_ages = (
        cycle.get("r3_heartbeat_max_age_seconds"),
        cycle.get("r5_heartbeat_max_age_seconds"),
    )
    if any(age is not None and age > limits.heartbeat_age_seconds for age in heartbeat_ages):
        failures.append("stale_heartbeat")
    if cycle.get("expected_output_sha256") != cycle.get("actual_output_sha256"):
        failures.append("output_mismatch")
    if cycle.get("expected_row_count") != cycle.get("actual_row_count"):
        failures.append("row_count_mismatch")
    if cycle.get("writer_clear_after") is False:
        failures.append("writer_contention")
    if cycle.get("locks_clear_after") is False:
        failures.append("lock_contention")
    if cycle.get("result") != "success" or cycle.get("exec_main_status") != 0:
        failures.append("service_failure")
    if missing:
        failures.append("incomplete_evidence")
    ordered = [name for name in FAILURE_ORDER if name in failures]
    return {
        "cycle_id": cycle.get("cycle_id"),
        "status": "PASSED" if not ordered else "FAILED",
        "failures": ordered,
        "missing_fields": missing,
        "evidence": cycle,
    }


def run_census(
    cycles: list[dict[str, Any]],
    *,
    previous_report: dict[str, Any] | None = None,
    limits: EvidenceLimits | None = None,
) -> dict[str, Any]:
    limits = limits or EvidenceLimits()
    prior = [] if previous_report is None else list(previous_report.get("cycle_assessments", []))
    assessments = list(prior)
    seen = {row.get("cycle_id") for row in prior}
    stopped_at: str | None = None
    duplicate_ids: list[str] = []
    if any(row.get("status") == "FAILED" for row in prior):
        stopped_at = next(row.get("cycle_id") for row in prior if row.get("status") == "FAILED")
    else:
        for raw in cycles:
            cycle_id = raw.get("cycle_id")
            if cycle_id in seen:
                duplicate_ids.append(str(cycle_id))
                continue
            assessment = assess_cycle(raw, limits)
            assessments.append(assessment)
            seen.add(cycle_id)
            if assessment["status"] == "FAILED":
                stopped_at = str(cycle_id)
                break
            if sum(row["status"] == "PASSED" for row in assessments) >= limits.required_cycles:
                break
    passed = sum(row["status"] == "PASSED" for row in assessments)
    failures = [failure for row in assessments for failure in row.get("failures", [])]
    status = "FAILED" if failures else "PASSED" if passed >= limits.required_cycles else "WAITING"
    report: dict[str, Any] = {
        "phase": "R5-RECOVERY-6A",
        "status": status,
        "mode": "LOCAL_SYNTHETIC_READ_ONLY",
        "cloud_access": False,
        "database_writes": 0,
        "service_changes": 0,
        "threshold_changes": 0,
        "execution_enabled": False,
        "required_cycles": limits.required_cycles,
        "passing_cycles": passed,
        "stopped_at_cycle": stopped_at,
        "duplicate_cycle_ids_ignored": sorted(set(duplicate_ids)),
        "cycle_assessments": assessments,
        "failure_summary": sorted(set(failures)),
        "decision": (
            "QUARANTINE_FAILED_CYCLE"
            if failures
            else "CERTIFIED_THREE_CYCLES"
            if passed >= limits.required_cycles
            else "WAIT_FOR_NEXT_DISTINCT_CYCLE"
        ),
        "next_phase": (
            "R5-RECOVERY-6B Preview — Scheduler Rollback and Failed-Cycle "
            "Quarantine Certification"
        ),
    }
    report["report_sha256"] = hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()
    return report


def write_report(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(report), encoding="utf-8")
    return path


def _canonical(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"


def _integer(value: Any) -> int | None:
    if value in (None, "", "[not set]"):
        return None
    return int(value)


def _number(value: Any) -> float | None:
    if value in (None, "", "[not set]"):
        return None
    return float(value)
