from __future__ import annotations

from kalshi_predictor.r5_recovery6b import (
    build_quarantine_record,
    build_rollback_plan,
    certify_quarantine,
)

ROLLBACK = "/mnt/backup/r5/code.tar.gz"
BACKUP = "/mnt/backup/r5/database.db"


def assessment(cycle_id: str, failures: list[str]):
    return {
        "cycle_id": cycle_id,
        "status": "FAILED" if failures else "PASSED",
        "failures": failures,
        "evidence": {"output_sha256": "abc", "execution_enabled": False},
    }


def test_failed_cycle_is_quarantined_immutably() -> None:
    first = build_quarantine_record(assessment("one", ["oom"]))
    second = build_quarantine_record(assessment("one", ["oom"]))
    assert first == second
    assert first["quarantined"] is True
    assert first["retry_allowed"] is False


def test_passing_cycle_is_not_quarantined() -> None:
    record = build_quarantine_record(assessment("one", []))
    assert record["quarantined"] is False
    assert record["runtime_activation_allowed"] is False


def test_rollback_plan_is_inert_and_forbids_restart() -> None:
    quarantine = build_quarantine_record(assessment("one", ["timeout"]))
    plan = build_rollback_plan(quarantine, rollback_bundle=ROLLBACK, backup_path=BACKUP)
    assert plan["commands_executable"] is False
    assert plan["automatic_restart_allowed"] is False
    assert plan["legacy_32_cycle_restart_allowed"] is False
    assert plan["evidence_complete"] is True


def test_missing_rollback_evidence_fails_certification() -> None:
    report = certify_quarantine(
        {"oom": assessment("one", ["oom"])}, rollback_bundle="", backup_path=BACKUP
    )
    assert report["status"] == "FAILED"
    assert report["failures"] == ["oom"]


def test_failure_matrix_passes_deterministically() -> None:
    scenarios = {
        name: assessment(name, [name])
        for name in (
            "execution_enabled",
            "oom",
            "timeout",
            "stale_heartbeat",
            "output_mismatch",
            "writer_contention",
            "lock_contention",
        )
    }
    scenarios["passing_control"] = assessment("passing", [])
    first = certify_quarantine(scenarios, rollback_bundle=ROLLBACK, backup_path=BACKUP)
    second = certify_quarantine(scenarios, rollback_bundle=ROLLBACK, backup_path=BACKUP)
    assert first == second
    assert first["status"] == "PASSED_LOCAL_PREVIEW"
    assert all(first["safety_properties"].values())
