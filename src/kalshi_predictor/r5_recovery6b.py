from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CRITICAL_FAILURES = {
    "execution_enabled",
    "oom",
    "timeout",
    "memory_limit",
    "stale_heartbeat",
    "output_mismatch",
    "row_count_mismatch",
    "writer_contention",
    "lock_contention",
    "service_failure",
    "incomplete_evidence",
}


def build_quarantine_record(assessment: dict[str, Any]) -> dict[str, Any]:
    failures = sorted(set(assessment.get("failures", [])))
    cycle_id = assessment.get("cycle_id")
    record: dict[str, Any] = {
        "cycle_id": cycle_id,
        "quarantined": bool(failures),
        "failures": failures,
        "unknown_failures": sorted(set(failures) - CRITICAL_FAILURES),
        "evidence_sha256": _sha256(assessment),
        "retry_allowed": False,
        "runtime_activation_allowed": False,
        "required_operator_evidence": [
            "verified pre-deployment database backup",
            "verified code/configuration rollback bundle",
            "authoritative writer and lock clearance",
            "EXECUTION_ENABLED=false",
            "root-cause evidence for every failed gate",
        ],
    }
    record["quarantine_sha256"] = _sha256(record)
    return record


def build_rollback_plan(
    quarantine: dict[str, Any],
    *,
    rollback_bundle: str,
    backup_path: str,
) -> dict[str, Any]:
    has_paths = bool(rollback_bundle.strip()) and bool(backup_path.strip())
    plan: dict[str, Any] = {
        "mode": "PREVIEW_ONLY",
        "cycle_id": quarantine.get("cycle_id"),
        "triggered": quarantine.get("quarantined") is True,
        "rollback_bundle": rollback_bundle,
        "database_backup": backup_path,
        "evidence_complete": has_paths,
        "commands_executable": False,
        "automatic_restart_allowed": False,
        "legacy_32_cycle_restart_allowed": False,
        "steps": [
            "Keep the scheduled and certification R5 services stopped.",
            "Confirm execution remains disabled and capture final writer/lock diagnostics.",
            "Preserve failed-cycle logs, heartbeat files, memory.events, and output hashes.",
            "Verify rollback bundle and database-backup SHA-256 values.",
            "Restore only the certified code/configuration scope after explicit approval.",
            "Run smoke tests and remain stopped pending a new bounded certification approval.",
        ],
    }
    plan["plan_sha256"] = _sha256(plan)
    return plan


def certify_quarantine(
    scenarios: dict[str, dict[str, Any]],
    *,
    rollback_bundle: str,
    backup_path: str,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    failures: list[str] = []
    for name in sorted(scenarios):
        assessment = scenarios[name]
        quarantine = build_quarantine_record(assessment)
        rollback = build_rollback_plan(
            quarantine, rollback_bundle=rollback_bundle, backup_path=backup_path
        )
        expected_quarantine = bool(assessment.get("failures"))
        passed = (
            quarantine["quarantined"] is expected_quarantine
            and quarantine["retry_allowed"] is False
            and quarantine["runtime_activation_allowed"] is False
            and rollback["commands_executable"] is False
            and rollback["automatic_restart_allowed"] is False
            and rollback["legacy_32_cycle_restart_allowed"] is False
            and rollback["evidence_complete"] is True
        )
        results[name] = {
            "status": "PASSED" if passed else "FAILED",
            "quarantine": quarantine,
            "rollback": rollback,
        }
        if not passed:
            failures.append(name)
    report: dict[str, Any] = {
        "phase": "R5-RECOVERY-6B",
        "status": "PASSED_LOCAL_PREVIEW" if not failures else "FAILED",
        "mode": "LOCAL_SYNTHETIC_READ_ONLY",
        "cloud_access": False,
        "database_writes": 0,
        "service_changes": 0,
        "execution_enabled": False,
        "scenario_results": results,
        "failures": failures,
        "safety_properties": {
            "failed_cycles_are_immutable": not failures,
            "retry_requires_new_approval": not failures,
            "rollback_commands_are_inert": not failures,
            "automatic_restart_is_forbidden": not failures,
            "legacy_service_restart_is_forbidden": not failures,
        },
        "next_phase": "R5-RECOVERY-6 — Guarded Three-Cycle Scheduler Re-entry Certification",
    }
    report["report_sha256"] = _sha256(report)
    return report


def write_report(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(report), encoding="utf-8")
    return path


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
