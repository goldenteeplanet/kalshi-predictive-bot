from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r16_cloud_service_install_handoff import (
    build_phase3bb_r16_cloud_service_install_handoff,
    write_phase3bb_r16_cloud_service_install_handoff_report,
)


def test_phase3bb_r16_writes_approved_handoff_bundle(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r16_cloud_service_install_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r16",
            reports_dir=reports_dir,
            operator_approved=True,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    script = artifacts.operator_handoff_script_path.read_text(encoding="utf-8")

    assert payload["phase"] == "3BB-R16-CLOUD-SERVICE-INSTALL-HANDOFF"
    assert payload["handoff_decision"]["status"] == "HANDOFF_READY_ENABLE_NO_START"
    assert payload["handoff_decision"]["handoff_ready"] is True
    assert payload["handoff_decision"]["codex_executed_install"] is False
    assert payload["handoff_decision"]["codex_executed_enable"] is False
    assert payload["handoff_decision"]["codex_executed_start"] is False
    assert payload["safety_flags"]["ssh_commands_executed"] == 0
    assert payload["safety_flags"]["systemctl_commands_executed"] == 0
    assert payload["safety_flags"]["starts_r5_watcher"] is False
    assert payload["safety_flags"]["stops_processes"] is False
    assert all(row["passed"] for row in payload["handoff_checks"])
    assert "systemctl enable kalshi-r5-watcher.service" in script
    assert "systemctl start" not in script
    assert "systemctl restart" not in script
    assert "PHASE3BB_R16_EXECUTE" in script
    assert artifacts.manifest_path.exists()


def test_phase3bb_r16_blocks_without_operator_approval(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r16_cloud_service_install_handoff(
            session,
            output_dir=reports_dir / "phase3bb_r16",
            reports_dir=reports_dir,
            operator_approved=False,
        )

    assert payload["handoff_decision"]["status"] == "BLOCKED_INSTALL_HANDOFF"
    assert payload["handoff_decision"]["handoff_ready"] is False
    assert payload["handoff_decision"]["first_failed_check"] == (
        "operator_approved_flag_present"
    )


def test_phase3bb_r16_handoff_script_defaults_to_dry_run(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r16_cloud_service_install_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r16",
            reports_dir=reports_dir,
            operator_approved=True,
        )

    result = subprocess.run(
        ["bash", str(artifacts.operator_handoff_script_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "dry-run command list" in result.stdout
    assert "no install/enable/start command executed" in result.stdout


def test_phase3bb_r16_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r16-cloud-service-install-handoff", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r16-cloud-service-install-handoff" in result.output
    assert "--operator-approved" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r16.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
    r13_dir = reports_dir / "phase3bb_r13"
    r13_dir.mkdir(parents=True, exist_ok=True)
    (r13_dir / "cloud_scheduler_adoption.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "cloud_target": {
                    "ssh_target": "kalshi@203.0.113.10",
                    "identity_file": "~/.ssh/id_ed25519_do",
                    "app_path": "/opt/kalshi-predictive-bot",
                    "env_path": "/etc/kalshi-bot/kalshi-bot.env",
                    "db_path": "/var/lib/kalshi-bot/kalshi_phase1.db",
                    "reports_path": "/opt/kalshi-predictive-bot/reports",
                },
                "adoption_decision": {
                    "recommendation": "ADOPT_EXISTING_R5",
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
                "cloud_target": {
                    "ssh_target": "kalshi@203.0.113.10",
                    "identity_file": "~/.ssh/id_ed25519_do",
                    "app_path": "/opt/kalshi-predictive-bot",
                    "env_path": "/etc/kalshi-bot/kalshi-bot.env",
                    "db_path": "/var/lib/kalshi-bot/kalshi_phase1.db",
                    "reports_path": "/opt/kalshi-predictive-bot/reports",
                },
                "service_plan": {
                    "status": "DRAFT_READY_FOR_REVIEW",
                    "existing_r5_pid": 1917,
                    "r13_recommendation": "ADOPT_EXISTING_R5",
                    "service_name": "kalshi-r5-watcher.service",
                    "guard_script_path": (
                        "/opt/kalshi-predictive-bot/scripts/cloud/"
                        "kalshi-r5-start-guard.sh"
                    ),
                    "remote_app_path": "/opt/kalshi-predictive-bot",
                    "remote_env_path": "/etc/kalshi-bot/kalshi-bot.env",
                    "remote_db_path": "/var/lib/kalshi-bot/kalshi_phase1.db",
                    "remote_reports_path": "/opt/kalshi-predictive-bot/reports",
                },
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
                "",
            ]
        ),
        encoding="utf-8",
    )

    r15_dir = reports_dir / "phase3bb_r15"
    r15_dir.mkdir(parents=True, exist_ok=True)
    (r15_dir / "cloud_service_install_review.json").write_text(
        json.dumps(
            {
                "install_review_decision": {
                    "status": "READY_FOR_OPERATOR_INSTALL_REVIEW_NO_START",
                    "failed_check_count": 0,
                    "ready_for_operator_review": True,
                }
            }
        ),
        encoding="utf-8",
    )
