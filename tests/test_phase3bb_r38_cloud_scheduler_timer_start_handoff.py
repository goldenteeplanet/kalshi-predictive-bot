from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    CloudBootstrapTarget,
    RemoteProbe,
    RemoteProbeResult,
)
from kalshi_predictor.phase3bb_r38_cloud_scheduler_timer_start_handoff import (
    build_phase3bb_r38_cloud_scheduler_timer_start_handoff,
    write_phase3bb_r38_cloud_scheduler_timer_start_handoff_report,
)


def test_phase3bb_r38_timer_start_handoff_ready_without_starting(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r38_cloud_scheduler_timer_start_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r38",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["timer_start_decision"]
    operator_script = artifacts.operator_handoff_script_path.read_text(encoding="utf-8")
    root_script = artifacts.root_console_script_path.read_text(encoding="utf-8")
    assert payload["phase"] == "3BB-R38-CLOUD-SCHEDULER-TIMER-START-HANDOFF"
    assert decision["status"] == "READY_FOR_OPERATOR_APPROVED_TIMER_START"
    assert decision["handoff_ready"] is True
    assert decision["scheduler_timer_enabled"] is True
    assert decision["scheduler_timer_active"] is False
    assert decision["r8_registered"] is True
    assert decision["codex_started_timer"] is False
    assert decision["codex_created_trades"] is False
    assert "PHASE3BB_R38_TIMER_START" in operator_script
    assert "systemctl start kalshi-multicategory-refresh-scheduler.timer" in operator_script
    assert "systemctl start kalshi-multicategory-refresh-scheduler.service" not in operator_script
    assert "systemctl status kalshi-multicategory-refresh-scheduler.timer" not in operator_script
    assert "systemctl start \"${TIMER}\"" in root_script
    assert "create-paper-trade" not in operator_script
    assert payload["safety_flags"]["scheduler_timer_started_by_codex"] is False
    assert artifacts.manifest_path.exists()


def test_phase3bb_r38_timer_start_blocks_when_r37_not_verified(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, r37_status="BLOCKED_SCHEDULER_INSTALL_VERIFICATION")

    with session_factory() as session:
        payload = build_phase3bb_r38_cloud_scheduler_timer_start_handoff(
            session,
            output_dir=reports_dir / "phase3bb_r38",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    decision = payload["timer_start_decision"]
    assert decision["status"] == "BLOCKED_TIMER_START_HANDOFF"
    assert decision["first_failed_check"] == "r37_verified_enable_no_start"


def test_phase3bb_r38_timer_start_handoff_defaults_to_dry_run(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r38_cloud_scheduler_timer_start_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r38",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    result = subprocess.run(
        ["bash", str(artifacts.operator_handoff_script_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "dry-run command list" in result.stdout
    assert "no timer start executed" in result.stdout


def test_phase3bb_r38_timer_start_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r38-cloud-scheduler-timer-start-handoff", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r38-cloud-scheduler-timer-start-handoff" in result.output
    assert "--per-probe-timeout-seconds" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r38_timer.db'}")
    return get_session_factory(engine)


def _write_context(
    reports_dir: Path,
    *,
    r37_status: str = "VERIFIED_SCHEDULER_INSTALL_ENABLE_NO_START",
) -> None:
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
    r37_dir = reports_dir / "phase3bb_r37"
    r37_dir.mkdir(parents=True, exist_ok=True)
    (r37_dir / "cloud_scheduler_install_verification.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "verification_decision": {
                    "status": r37_status,
                    "verification_passed": r37_status
                    == "VERIFIED_SCHEDULER_INSTALL_ENABLE_NO_START",
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_probe_runner(
    overrides: dict[str, tuple[str, bool, int | None, str]] | None = None,
):
    outputs = {
        "scheduler_systemd_state": (
            "\n".join(
                [
                    "Id=kalshi-multicategory-refresh-scheduler.service",
                    "LoadState=loaded",
                    "UnitFileState=static",
                    "ActiveState=inactive",
                    "SubState=dead",
                    "ExecMainPID=0",
                    "Id=kalshi-multicategory-refresh-scheduler.timer",
                    "LoadState=loaded",
                    "UnitFileState=enabled",
                    "ActiveState=inactive",
                    "SubState=dead",
                    "ExecMainPID=0",
                    "",
                ]
            ),
            True,
            0,
            "",
        ),
        "scheduler_timer_enabled": ("enabled\n", True, 0, ""),
        "scheduler_timer_active": ("inactive\n", True, 0, ""),
        "scheduler_service_active": ("inactive\n", True, 0, ""),
        "r8_command_registry": ("R8_REGISTERED\n", True, 0, ""),
        "sudo_noninteractive_true": ("SUDO_N_BLOCKED\n", True, 0, ""),
        "r5_status": (
            json.dumps(
                {
                    "pid": 10573,
                    "process": {
                        "phase3bc_r5_process_running": True,
                        "phase3bc_r5_pids": [10573],
                    },
                    "guard": {"status": "RUNNING", "should_stop": False},
                    "latest_watch_state": "WAITING_FOR_POSITIVE_EV",
                    "latest_summary": {
                        "positive_ev_rows": 0,
                        "paper_ready_candidates": 0,
                    },
                }
            ),
            True,
            0,
            "",
        ),
        "db_writer_monitor": (
            json.dumps(
                {
                    "status": "OPEN_READERS",
                    "safe_to_start_write": True,
                    "current_writer_pid": None,
                }
            ),
            True,
            0,
            "",
        ),
    }
    outputs.update(overrides or {})

    def run(probe: RemoteProbe, _target: CloudBootstrapTarget) -> RemoteProbeResult:
        stdout, ok, exit_code, stderr = outputs[probe.name]
        return RemoteProbeResult(
            name=probe.name,
            command=probe.command,
            ok=ok,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.01,
        )

    return run
