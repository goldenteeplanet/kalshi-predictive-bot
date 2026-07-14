from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r22_cloud_ui_install_handoff import (
    build_phase3bb_r22_cloud_ui_install_handoff,
    write_phase3bb_r22_cloud_ui_install_handoff_report,
)


def test_phase3bb_r22_writes_approved_ui_handoff_bundle(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r22_cloud_ui_install_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r22",
            reports_dir=reports_dir,
            operator_approved=True,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    script = artifacts.operator_handoff_script_path.read_text(encoding="utf-8")

    assert payload["phase"] == "3BB-R22-CLOUD-UI-INSTALL-HANDOFF"
    assert payload["handoff_decision"]["status"] == (
        "HANDOFF_READY_UI_INSTALL_ENABLE_NO_START"
    )
    assert payload["handoff_decision"]["handoff_ready"] is True
    assert payload["handoff_decision"]["codex_executed_install"] is False
    assert payload["handoff_decision"]["codex_executed_enable"] is False
    assert payload["handoff_decision"]["codex_executed_start"] is False
    assert payload["safety_flags"]["ssh_commands_executed"] == 0
    assert payload["safety_flags"]["systemctl_commands_executed"] == 0
    assert payload["safety_flags"]["starts_ui_service"] is False
    assert payload["safety_flags"]["stops_processes"] is False
    assert all(row["passed"] for row in payload["handoff_checks"])
    assert "systemctl enable kalshi-ui.service" in script
    assert "systemctl start" not in script
    assert "systemctl restart" not in script
    assert "ufw allow" not in script
    assert "PHASE3BB_R22_EXECUTE" in script
    assert artifacts.manifest_path.exists()


def test_phase3bb_r22_blocks_without_operator_approval(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r22_cloud_ui_install_handoff(
            session,
            output_dir=reports_dir / "phase3bb_r22",
            reports_dir=reports_dir,
            operator_approved=False,
        )

    assert payload["handoff_decision"]["status"] == "BLOCKED_UI_INSTALL_HANDOFF"
    assert payload["handoff_decision"]["handoff_ready"] is False
    assert payload["handoff_decision"]["first_failed_check"] == (
        "operator_approved_flag_present"
    )


def test_phase3bb_r22_handoff_script_defaults_to_dry_run(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r22_cloud_ui_install_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r22",
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


def test_phase3bb_r22_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r22-cloud-ui-install-handoff", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r22-cloud-ui-install-handoff" in result.output
    assert "--operator-approved" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r22.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
    generated_at = datetime.now(UTC).isoformat()
    r20_dir = reports_dir / "phase3bb_r20"
    r20_dir.mkdir(parents=True, exist_ok=True)
    ui_plan = {
        "status": "DRAFT_READY_FOR_REVIEW",
        "r18_status": "SYSTEMD_OWNS_R5",
        "r5_pid": 23133,
        "ssh_target": "kalshi@203.0.113.10",
        "identity_file": "~/.ssh/id_ed25519_do",
        "remote_app_path": "/opt/kalshi-predictive-bot",
        "remote_env_path": "/etc/kalshi-bot/kalshi-bot.env",
        "remote_db_path": "/var/lib/kalshi-bot/kalshi_phase1.db",
        "remote_reports_path": "/opt/kalshi-predictive-bot/reports",
        "ssh_tunnel_command": (
            "ssh -i '~/.ssh/id_ed25519_do' -L 8080:127.0.0.1:8080 "
            "'kalshi@203.0.113.10'"
        ),
    }
    (r20_dir / "cloud_ui_service_plan.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "ui_service_plan": ui_plan,
                "parsed_ui_state": {
                    "ui_duplicate_process": False,
                    "service_started": False,
                },
            }
        ),
        encoding="utf-8",
    )
    (r20_dir / "kalshi-ui.service.draft").write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Kalshi Bot UI",
                "After=kalshi-r5-watcher.service",
                "[Service]",
                "User=kalshi",
                "EnvironmentFile=/etc/kalshi-bot/kalshi-bot.env",
                "Environment=UI_READ_ONLY=true",
                "Environment=EXECUTION_ENABLED=false",
                "Environment=EXECUTION_KILL_SWITCH=true",
                "ExecStart=/opt/kalshi-predictive-bot/.venv/bin/kalshi-bot ui "
                "--host 127.0.0.1 --port 8080",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (r20_dir / "kalshi-ui.nginx.draft").write_text(
        "# deferred - do not install until public exposure is reviewed\n",
        encoding="utf-8",
    )

    r21_dir = reports_dir / "phase3bb_r21"
    r21_dir.mkdir(parents=True, exist_ok=True)
    (r21_dir / "cloud_ui_install_review.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "ui_service_plan": ui_plan,
                "install_review_decision": {
                    "status": "READY_FOR_OPERATOR_UI_INSTALL_REVIEW_NO_START",
                    "failed_check_count": 0,
                    "ready_for_operator_review": True,
                    "r5_pid": 23133,
                },
            }
        ),
        encoding="utf-8",
    )
