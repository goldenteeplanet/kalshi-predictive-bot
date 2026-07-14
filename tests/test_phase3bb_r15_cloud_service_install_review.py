from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r15_cloud_service_install_review import (
    build_phase3bb_r15_cloud_service_install_review,
    write_phase3bb_r15_cloud_service_install_review_report,
)


def test_phase3bb_r15_writes_no_start_install_review(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, recommendation="ADOPT_EXISTING_R5")

    with session_factory() as session:
        artifacts = write_phase3bb_r15_cloud_service_install_review_report(
            session,
            output_dir=reports_dir / "phase3bb_r15",
            reports_dir=reports_dir,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    dry_run = artifacts.no_start_dry_run_path.read_text(encoding="utf-8")
    operator_command = artifacts.operator_command_path.read_text(encoding="utf-8")

    assert payload["phase"] == "3BB-R15-CLOUD-SERVICE-INSTALL-REVIEW-NO-START"
    assert (
        payload["install_review_decision"]["status"]
        == "READY_FOR_OPERATOR_INSTALL_REVIEW_NO_START"
    )
    assert payload["install_review_decision"]["install_allowed_now"] is False
    assert payload["install_review_decision"]["start_allowed_now"] is False
    assert payload["install_review_decision"]["copy_to_remote_allowed_now"] is False
    assert payload["safety_flags"]["no_service_install"] is True
    assert payload["safety_flags"]["systemctl_commands_executed"] == 0
    assert payload["safety_flags"]["ssh_commands_executed"] == 0
    assert payload["safety_flags"]["starts_r5_watcher"] is False
    assert payload["safety_flags"]["stops_processes"] is False
    assert payload["install_review_decision"]["current_r5_pid"] == 1917
    assert all(row["passed"] for row in payload["review_checks"])
    assert "systemctl" not in dry_run
    assert "scp " not in dry_run
    assert "phase3bb-r13-cloud-scheduler-adoption" in operator_command
    assert artifacts.review_csv_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3bb_r15_blocks_when_r13_no_longer_adopts(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, recommendation="WAIT")

    with session_factory() as session:
        payload = build_phase3bb_r15_cloud_service_install_review(
            session,
            output_dir=reports_dir / "phase3bb_r15",
            reports_dir=reports_dir,
        )

    assert payload["install_review_decision"]["status"] == "BLOCKED_INSTALL_REVIEW"
    assert payload["install_review_decision"]["ready_for_operator_review"] is False
    assert payload["install_review_decision"]["install_allowed_now"] is False
    assert payload["install_review_decision"]["first_failed_check"] == (
        "r13_adopts_existing_r5"
    )


def test_phase3bb_r15_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r15-cloud-service-install-review", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r15-cloud-service-install-review" in result.output
    assert "--r13-max-age-minutes" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r15.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path, *, recommendation: str) -> None:
    r13_dir = reports_dir / "phase3bb_r13"
    r13_dir.mkdir(parents=True, exist_ok=True)
    (r13_dir / "cloud_scheduler_adoption.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "adoption_decision": {
                    "recommendation": recommendation,
                    "current_r5_pid": 1917,
                    "guard_status": "RUNNING",
                    "guard_should_stop": False,
                    "duplicate_r5": False,
                    "writer_matches_r5": True,
                    "watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
                },
            }
        ),
        encoding="utf-8",
    )

    r14_dir = reports_dir / "phase3bb_r14"
    r14_dir.mkdir(parents=True, exist_ok=True)
    (r14_dir / "cloud_service_plan.json").write_text(
        json.dumps(
            {
                "service_plan": {
                    "status": "DRAFT_READY_FOR_REVIEW",
                    "existing_r5_pid": 1917,
                    "r13_recommendation": recommendation,
                    "service_name": "kalshi-r5-watcher.service",
                    "install_allowed_now": False,
                    "start_allowed_now": False,
                    "enable_allowed_now": False,
                }
            }
        ),
        encoding="utf-8",
    )
    (r14_dir / "kalshi-r5-watcher.service.draft").write_text(
        "\n".join(
            [
                "[Service]",
                "User=kalshi",
                "EnvironmentFile=/etc/kalshi-bot/kalshi-bot.env",
                "ExecStartPre=/opt/kalshi-predictive-bot/scripts/cloud/"
                "kalshi-r5-start-guard.sh",
                "ExecStart=/opt/kalshi-predictive-bot/.venv/bin/python "
                "-m kalshi_predictor.cli phase3bc-r5-crypto-freshness-watch",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (r14_dir / "kalshi-r5-start-guard.sh.draft").write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "existing_pids=$(pgrep -f 'phase3bc-r5-crypto-freshness-watch' || true)",
                "echo 'Refusing duplicate R5 start'",
                ".venv/bin/kalshi-bot db-writer-monitor --json",
                "grep -q '\"safe_to_start_write\": false' /tmp/guard.json",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (r14_dir / "install_review_checklist.md").write_text(
        "# Phase 3BB-R14 Install Review Checklist\n\n## R14 Is Draft Only\n",
        encoding="utf-8",
    )
