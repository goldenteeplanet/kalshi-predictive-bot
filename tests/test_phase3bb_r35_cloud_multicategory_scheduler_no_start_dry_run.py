from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run import (
    build_phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run,
    write_phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run_report,
)


def test_phase3bb_r35_writes_no_start_scheduler_drafts(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r34_artifact(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run_report(
            session,
            output_dir=reports_dir / "phase3bb_r35",
            reports_dir=reports_dir,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["dry_run_decision"]
    runner = artifacts.runner_draft_path.read_text(encoding="utf-8")
    no_start = artifacts.no_start_dry_run_path.read_text(encoding="utf-8")
    assert payload["phase"] == "3BB-R35-CLOUD-MULTICATEGORY-SCHEDULER-NO-START-DRY-RUN"
    assert decision["status"] == "READY_FOR_OPERATOR_APPROVED_SCHEDULER_INSTALL_HANDOFF"
    assert decision["dry_run_passed"] is True
    assert decision["job_count"] == 3
    assert decision["writer_gated_job_count"] == 1
    assert "db-writer-monitor --json" in runner
    assert "Writer active; skip writer-gated job" in runner
    assert "Writer became active during" in runner
    assert "Status: BUSY_WRITER|Database is busy" in runner
    assert "db-writer-monitor JSON parse failed; skip writer-gated job" in runner
    assert "json.load(sys.stdin)" not in runner
    assert "phase3bb-r2-weather-fast-lane" in runner
    assert "systemctl start" not in no_start
    assert "systemctl enable" not in no_start
    assert "systemctl restart" not in no_start
    assert payload["safety_flags"]["starts_scheduler"] is False
    assert payload["safety_flags"]["runs_refresh_jobs"] is False
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert artifacts.manifest_path.exists()


def test_phase3bb_r35_blocks_stale_r34_artifact(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r34_artifact(reports_dir, generated_at=datetime.now(UTC) - timedelta(hours=3))

    with session_factory() as session:
        payload = build_phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run(
            session,
            output_dir=reports_dir / "phase3bb_r35",
            reports_dir=reports_dir,
            r34_max_age_minutes=30,
        )

    decision = payload["dry_run_decision"]
    assert decision["status"] == "BLOCKED_SCHEDULER_NO_START_DRY_RUN"
    assert decision["first_failed_check"] == "r34_recent_enough"


def test_phase3bb_r35_blocks_forbidden_scheduler_command(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    jobs = _jobs()
    jobs.append(
        {
            "job_id": "bad_trade_job",
            "category": "unsafe",
            "cadence_minutes": 15,
            "priority": 99,
            "command": "kalshi-bot place-order --ticker KXTEST",
            "writer_capable": False,
            "requires_db_writer_gate": False,
            "max_runtime_seconds": 30,
            "enabled_in_draft": True,
        }
    )
    _write_r34_artifact(reports_dir, jobs=jobs)

    with session_factory() as session:
        payload = build_phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run(
            session,
            output_dir=reports_dir / "phase3bb_r35",
            reports_dir=reports_dir,
        )

    decision = payload["dry_run_decision"]
    failed = {row["check"] for row in payload["dry_run_checks"] if not row["passed"]}
    assert decision["status"] == "BLOCKED_SCHEDULER_NO_START_DRY_RUN"
    assert "no_forbidden_trade_or_service_fragments" in failed


def test_phase3bb_r35_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r35-cloud-multicategory-scheduler-no-start-dry-run", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r35-cloud-multicategory-scheduler-no-start-dry-run" in result.output
    assert "--r34-max-age-minutes" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r35.db'}")
    return get_session_factory(engine)


def _write_r34_artifact(
    reports_dir: Path,
    *,
    generated_at: datetime | None = None,
    jobs: list[dict[str, object]] | None = None,
) -> None:
    r34_dir = reports_dir / "phase3bb_r34"
    r34_dir.mkdir(parents=True, exist_ok=True)
    generated = generated_at or datetime.now(UTC)
    payload = {
        "generated_at": generated.isoformat(),
        "scheduler_decision": {
            "status": "READY_FOR_NO_START_SCHEDULER_DRY_RUN",
            "review_passed": True,
            "r5_pid": 23133,
            "watch_state": "WAITING_FOR_POSITIVE_EV",
            "paper_ready_candidates": 0,
        },
        "refresh_jobs": jobs if jobs is not None else _jobs(),
    }
    (r34_dir / "cloud_multicategory_refresh_scheduler_review.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _jobs() -> list[dict[str, object]]:
    return [
        {
            "job_id": "operations_readiness_monitor",
            "category": "system",
            "cadence_minutes": 15,
            "priority": 10,
            "command": (
                "kalshi-bot phase3bb-r33-cloud-paper-only-operations-readiness "
                "--output-dir reports/phase3bb_r33 --reports-dir reports"
            ),
            "writer_capable": False,
            "requires_db_writer_gate": False,
            "max_runtime_seconds": 90,
            "enabled_in_draft": True,
        },
        {
            "job_id": "unified_paper_gate",
            "category": "all",
            "cadence_minutes": 15,
            "priority": 20,
            "command": (
                "kalshi-bot phase3bb-r8-unified-paper-gate "
                "--output-dir reports/phase3bb_r8 --reports-dir reports"
            ),
            "writer_capable": False,
            "requires_db_writer_gate": False,
            "max_runtime_seconds": 90,
            "enabled_in_draft": True,
        },
        {
            "job_id": "weather_fast_lane",
            "category": "weather",
            "cadence_minutes": 30,
            "priority": 30,
            "command": (
                "kalshi-bot phase3bb-r2-weather-fast-lane "
                "--output-dir reports/phase3bb_r2 --reports-dir reports"
            ),
            "writer_capable": True,
            "requires_db_writer_gate": True,
            "max_runtime_seconds": 120,
            "enabled_in_draft": True,
        },
    ]
