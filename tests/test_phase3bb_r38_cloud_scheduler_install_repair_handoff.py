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
from kalshi_predictor.phase3bb_r38_cloud_scheduler_install_repair_handoff import (
    build_phase3bb_r38_cloud_scheduler_install_repair_handoff,
    write_phase3bb_r38_cloud_scheduler_install_repair_handoff_report,
)


def test_phase3bb_r38_writes_repair_handoff_for_sudo_block_and_missing_r8(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r38_cloud_scheduler_install_repair_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r38",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["repair_decision"]
    root_script = artifacts.root_console_script_path.read_text(encoding="utf-8")
    sync_script = artifacts.code_sync_handoff_script_path.read_text(encoding="utf-8")
    assert payload["phase"] == "3BB-R38-CLOUD-SCHEDULER-INSTALL-REPAIR-HANDOFF"
    assert decision["status"] == "REPAIR_HANDOFF_READY_NO_START"
    assert decision["handoff_ready"] is True
    assert decision["tmp_scheduler_files_present"] is True
    assert decision["needs_code_sync"] is True
    assert decision["needs_root_console_install"] is True
    assert decision["codex_executed_code_sync"] is False
    assert decision["codex_executed_root_install"] is False
    assert decision["codex_started_scheduler"] is False
    assert "systemctl enable \"${TIMER}\"" in root_script
    assert "systemctl start" not in root_script
    assert "systemctl restart" not in root_script
    assert "PHASE3BB_R38_CODE_SYNC" in sync_script
    assert "phase3bb-r8-unified-paper-gate --help" in sync_script
    assert payload["safety_flags"]["scheduler_timer_started"] is False
    assert payload["safety_flags"]["code_sync_executed_by_codex"] is False
    assert artifacts.manifest_path.exists()


def test_phase3bb_r38_blocks_when_tmp_scheduler_files_missing(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    runner = _fake_probe_runner(
        {
            "tmp_scheduler_files": (
                "\n".join(
                    [
                        "/tmp/kalshi-multicategory-refresh-scheduler.service MISSING",
                        "/tmp/kalshi-multicategory-refresh-scheduler.timer PRESENT",
                        "/tmp/kalshi-multicategory-refresh-runner.sh PRESENT",
                        "",
                    ]
                ),
                True,
                0,
                "",
            )
        }
    )

    with session_factory() as session:
        payload = build_phase3bb_r38_cloud_scheduler_install_repair_handoff(
            session,
            output_dir=reports_dir / "phase3bb_r38",
            reports_dir=reports_dir,
            probe_runner=runner,
        )

    decision = payload["repair_decision"]
    assert decision["status"] == "BLOCKED_REPAIR_HANDOFF"
    assert decision["first_failed_check"] == "tmp_scheduler_files_present"
    assert "PHASE3BB_R36_EXECUTE=I_APPROVE_R36_SCHEDULER_INSTALL" in (
        decision["operator_next_command"]
    )


def test_phase3bb_r38_code_sync_handoff_defaults_to_dry_run(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r38_cloud_scheduler_install_repair_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r38",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    result = subprocess.run(
        ["bash", str(artifacts.code_sync_handoff_script_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "dry-run command list" in result.stdout
    assert "no code sync or remote copy executed" in result.stdout


def test_phase3bb_r38_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r38-cloud-scheduler-install-repair-handoff", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r38-cloud-scheduler-install-repair-handoff" in result.output
    assert "--per-probe-timeout-seconds" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r38.db'}")
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

    r37_dir = reports_dir / "phase3bb_r37"
    r37_dir.mkdir(parents=True, exist_ok=True)
    (r37_dir / "cloud_scheduler_install_verification.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "verification_decision": {
                    "status": "BLOCKED_SCHEDULER_INSTALL_VERIFICATION",
                    "first_failed_check": "scheduler_service_unit_installed",
                    "current_r5_pid": 10573,
                    "command_registry_missing_command": "phase3bb-r8-unified-paper-gate",
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_probe_runner(
    overrides: dict[str, tuple[str, bool, int | None, str]] | None = None,
):
    outputs = {
        "tmp_scheduler_files": (
            "\n".join(
                [
                    "/tmp/kalshi-multicategory-refresh-scheduler.service PRESENT",
                    "/tmp/kalshi-multicategory-refresh-scheduler.timer PRESENT",
                    "/tmp/kalshi-multicategory-refresh-runner.sh PRESENT",
                    "",
                ]
            ),
            True,
            0,
            "",
        ),
        "app_writable": ("APP_WRITABLE\n", True, 0, ""),
        "venv_pip": ("pip 24.0 from .venv/lib/python3.12/site-packages/pip\nVENV_PIP_OK\n", True, 0, ""),
        "r8_command_registry": (
            "",
            False,
            2,
            "No such command 'phase3bb-r8-unified-paper-gate'.",
        ),
        "scheduler_systemd_state": (
            "\n".join(
                [
                    "Id=kalshi-multicategory-refresh-scheduler.service",
                    "LoadState=not-found",
                    "UnitFileState=",
                    "ActiveState=inactive",
                    "SubState=dead",
                    "ExecMainPID=0",
                    "Id=kalshi-multicategory-refresh-scheduler.timer",
                    "LoadState=not-found",
                    "UnitFileState=",
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
                    "status": "WRITER_ACTIVE",
                    "safe_to_start_write": False,
                    "current_writer_pid": 42336,
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
