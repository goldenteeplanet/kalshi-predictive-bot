from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r36_cloud_scheduler_install_handoff import (
    build_phase3bb_r36_cloud_scheduler_install_handoff,
    write_phase3bb_r36_cloud_scheduler_install_handoff_report,
)


def test_phase3bb_r36_writes_approved_scheduler_handoff(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r36_cloud_scheduler_install_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r36",
            reports_dir=reports_dir,
            operator_approved=True,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    script = artifacts.operator_handoff_script_path.read_text(encoding="utf-8")
    decision = payload["handoff_decision"]
    assert payload["phase"] == "3BB-R36-CLOUD-SCHEDULER-INSTALL-HANDOFF"
    assert decision["status"] == "HANDOFF_READY_SCHEDULER_INSTALL_ENABLE_NO_START"
    assert decision["handoff_ready"] is True
    assert decision["codex_executed_install"] is False
    assert decision["codex_executed_enable"] is False
    assert decision["codex_executed_start"] is False
    assert payload["safety_flags"]["ssh_commands_executed"] == 0
    assert payload["safety_flags"]["systemctl_commands_executed"] == 0
    assert payload["safety_flags"]["starts_scheduler"] is False
    assert payload["safety_flags"]["runs_refresh_jobs"] is False
    assert all(row["passed"] for row in payload["handoff_checks"])
    assert "PHASE3BB_R36_EXECUTE" in script
    assert "systemctl enable kalshi-multicategory-refresh-scheduler.timer" in script
    assert "systemctl start" not in script
    assert "phase3bc-r5-unattended-start" not in script
    assert artifacts.manifest_path.exists()


def test_phase3bb_r36_blocks_without_operator_approval(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r36_cloud_scheduler_install_handoff(
            session,
            output_dir=reports_dir / "phase3bb_r36",
            reports_dir=reports_dir,
            operator_approved=False,
        )

    decision = payload["handoff_decision"]
    assert decision["status"] == "BLOCKED_SCHEDULER_INSTALL_HANDOFF"
    assert decision["handoff_ready"] is False
    assert decision["first_failed_check"] == "operator_approved_flag_present"


def test_phase3bb_r36_handoff_script_defaults_to_dry_run(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r36_cloud_scheduler_install_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r36",
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


def test_phase3bb_r36_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r36-cloud-scheduler-install-handoff", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r36-cloud-scheduler-install-handoff" in result.output
    assert "--operator-approved" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r36.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
    r11_dir = reports_dir / "phase3bb_r11"
    r11_dir.mkdir(parents=True, exist_ok=True)
    (r11_dir / "codex_cloud_context.json").write_text(
        json.dumps(
            {
                "ssh_profile": {
                    "host": "203.0.113.10",
                    "user": "kalshi",
                    "identity_file": "~/.ssh/id_ed25519_do",
                },
                "remote_paths": {
                    "app_path": "/opt/kalshi-predictive-bot",
                    "env_path": "/etc/kalshi-bot/kalshi-bot.env",
                    "db_path": "/var/lib/kalshi-bot/kalshi_phase1.db",
                    "reports_path": "/opt/kalshi-predictive-bot/reports",
                },
            }
        ),
        encoding="utf-8",
    )

    r35_dir = reports_dir / "phase3bb_r35"
    r35_dir.mkdir(parents=True, exist_ok=True)
    (r35_dir / "cloud_multicategory_scheduler_no_start_dry_run.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "dry_run_decision": {
                    "status": "READY_FOR_OPERATOR_APPROVED_SCHEDULER_INSTALL_HANDOFF",
                    "dry_run_passed": True,
                    "failed_check_count": 0,
                    "r5_pid": 23133,
                    "watch_state": "WAITING_FOR_POSITIVE_EV",
                    "paper_ready_candidates": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    (r35_dir / "kalshi-multicategory-refresh-scheduler.service.draft").write_text(
        "\n".join(
            [
                "[Service]",
                "Type=oneshot",
                "User=kalshi",
                "ExecStart=/opt/kalshi-predictive-bot/scripts/"
                "kalshi-multicategory-refresh-runner.sh",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (r35_dir / "kalshi-multicategory-refresh-scheduler.timer.draft").write_text(
        "\n".join(
            [
                "[Timer]",
                "OnUnitActiveSec=15min",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (r35_dir / "kalshi-multicategory-refresh-runner.sh.draft").write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "writer_clear() {",
                "  .venv/bin/kalshi-bot db-writer-monitor --json",
                "}",
                "echo '[phase3bb-r35] Writer active; skip writer-gated job weather'",
                ".venv/bin/kalshi-bot phase3bb-r33-cloud-paper-only-operations-readiness",
                "",
            ]
        ),
        encoding="utf-8",
    )
