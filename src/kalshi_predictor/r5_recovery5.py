from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SchedulerPolicy:
    persistent: bool = False
    max_runtime_seconds: int = 2700
    stale_heartbeat_seconds: int = 30
    restart: str = "no"
    execution_enabled: bool = False


def simulate_scheduler(
    events: list[dict[str, Any]], policy: SchedulerPolicy | None = None
) -> dict[str, Any]:
    policy = policy or SchedulerPolicy()
    active_run: str | None = None
    completed_runs: set[str] = set()
    decisions: list[dict[str, Any]] = []
    violations: list[str] = []

    for index, event in enumerate(events):
        kind = str(event.get("kind", "unknown"))
        event_id = str(event.get("event_id", f"event-{index + 1}"))
        decision = {
            "event_id": event_id,
            "kind": kind,
            "action": "IGNORE",
            "reason": "non-scheduling event",
            "active_run_before": active_run,
        }

        if event.get("execution_enabled") is True or policy.execution_enabled:
            decision.update(action="BLOCK", reason="execution must remain disabled")
            violations.append("execution_enabled")
        elif kind in {"timer", "missed_timer_replay", "restart_recovery"}:
            candidate = str(event.get("run_id", event_id))
            writer_safe = event.get("writer_safe") is True
            locks_clear = event.get("locks_clear") is True
            missed_allowed = kind != "missed_timer_replay" or policy.persistent
            heartbeat_fresh = float(event.get("heartbeat_age_seconds", 0)) <= (
                policy.stale_heartbeat_seconds
            )
            if candidate in completed_runs:
                decision.update(action="SKIP", reason="run already completed")
            elif not missed_allowed:
                decision.update(action="SKIP", reason="persistent replay disabled")
            elif active_run is not None:
                decision.update(action="BLOCK", reason="service already active")
            elif not writer_safe or not locks_clear:
                decision.update(action="BLOCK", reason="writer or lock gate not clear")
            elif kind == "restart_recovery" and not heartbeat_fresh:
                decision.update(action="BLOCK", reason="restart evidence is stale")
            else:
                active_run = candidate
                decision.update(action="START", reason="all fail-closed gates passed")
        elif kind == "complete":
            run_id = str(event.get("run_id", ""))
            if active_run == run_id and event.get("output_valid") is True:
                completed_runs.add(run_id)
                active_run = None
                decision.update(action="COMPLETE", reason="valid completion evidence")
            else:
                decision.update(action="BLOCK", reason="invalid or unmatched completion")
                violations.append("invalid_completion")
        elif kind in {"timeout", "oom", "heartbeat_stale"}:
            run_id = str(event.get("run_id", ""))
            if active_run == run_id:
                active_run = None
            decision.update(action="FAIL_CLOSED", reason=kind)
            violations.append(kind)

        decision["active_run_after"] = active_run
        decisions.append(decision)

    starts = [row for row in decisions if row["action"] == "START"]
    duplicate_starts = len({row["active_run_after"] for row in starts}) != len(starts)
    if duplicate_starts:
        violations.append("duplicate_start")
    return {
        "policy": {
            "persistent": policy.persistent,
            "max_runtime_seconds": policy.max_runtime_seconds,
            "stale_heartbeat_seconds": policy.stale_heartbeat_seconds,
            "restart": policy.restart,
            "execution_enabled": policy.execution_enabled,
        },
        "decisions": decisions,
        "started_runs": [row["active_run_after"] for row in starts],
        "completed_runs": sorted(completed_runs),
        "active_run_at_end": active_run,
        "violations": sorted(set(violations)),
        "sole_writer_preserved": not duplicate_starts,
    }


def build_certification_report(scenarios: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    expected = {
        "normal": {"violations": [], "completed": 1},
        "overlap": {"violations": [], "blocked_reason": "service already active"},
        "writer_busy": {"violations": [], "blocked_reason": "writer or lock gate not clear"},
        "missed_schedule": {"violations": [], "blocked_reason": "persistent replay disabled"},
        "restart": {"violations": [], "completed": 1},
        "stale_restart": {"violations": [], "blocked_reason": "restart evidence is stale"},
        "timeout": {"violations": ["timeout"]},
        "oom": {"violations": ["oom"]},
    }
    results: dict[str, Any] = {}
    failures: list[str] = []
    for name in sorted(expected):
        if name not in scenarios:
            failures.append(f"missing_scenario:{name}")
            continue
        result = simulate_scheduler(scenarios[name])
        rule = expected[name]
        reasons = {
            row["reason"]
            for row in result["decisions"]
            if row["action"] in {"BLOCK", "SKIP"}
        }
        passed = result["violations"] == rule["violations"]
        if "completed" in rule:
            passed = passed and len(result["completed_runs"]) == rule["completed"]
        if "blocked_reason" in rule:
            passed = passed and rule["blocked_reason"] in reasons
        if not result["sole_writer_preserved"]:
            passed = False
        results[name] = {"status": "PASSED" if passed else "FAILED", **result}
        if not passed:
            failures.append(name)

    status = "PASSED_LOCAL_PREVIEW" if not failures else "FAILED"
    report: dict[str, Any] = {
        "phase": "R5-RECOVERY-5",
        "status": status,
        "mode": "LOCAL_SYNTHETIC_READ_ONLY",
        "cloud_access": False,
        "database_writes": 0,
        "service_changes": 0,
        "threshold_changes": 0,
        "execution_enabled": False,
        "certification_gates": {
            "sole_writer_enforced": not failures,
            "overlap_blocked": results.get("overlap", {}).get("status") == "PASSED",
            "missed_schedule_fail_closed": results.get("missed_schedule", {}).get("status")
            == "PASSED",
            "restart_requires_fresh_evidence": results.get("stale_restart", {}).get("status")
            == "PASSED",
            "timeout_and_oom_fail_closed": all(
                results.get(name, {}).get("status") == "PASSED" for name in ("timeout", "oom")
            ),
        },
        "scenario_results": results,
        "failures": failures,
        "reentry_design": {
            "service_shape": "one bounded cycle per invocation",
            "timer_persistent": False,
            "service_restart": "no",
            "start_requirements": [
                "EXECUTION_ENABLED=false",
                "db-writer-monitor safe_to_start_write=true",
                "db-locks clear of writers",
                "no active R5 certification or scheduled invocation",
                "fresh prior completion or restart evidence",
            ],
            "completion_requirements": [
                "valid output parity",
                "fresh R3/R5 heartbeat",
                "no timeout or OOM",
                "post-cycle writer and locks clear",
            ],
        },
        "rollback_recommendation": (
            "HOLD_AND_KEEP_SCHEDULER_DISABLED"
            if failures
            else "READY_FOR_GUARDED_SCHEDULER_REENTRY_CERTIFICATION"
        ),
        "next_phase": "R5-RECOVERY-6 — Guarded Three-Cycle Scheduler Re-entry Certification",
    }
    report["report_sha256"] = hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()
    return report


def write_report(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(report), encoding="utf-8")
    return path


def _canonical(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
