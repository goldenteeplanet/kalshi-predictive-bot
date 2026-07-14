from __future__ import annotations

import json
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
from kalshi_predictor.phase3bb_r37_cloud_scheduler_install_verification import (
    build_phase3bb_r37_cloud_scheduler_install_verification,
    write_phase3bb_r37_cloud_scheduler_install_verification_report,
)


def test_phase3bb_r37_verifies_scheduler_install_enable_no_start(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r37_cloud_scheduler_install_verification_report(
            session,
            output_dir=reports_dir / "phase3bb_r37",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["verification_decision"]
    assert payload["phase"] == "3BB-R37-CLOUD-SCHEDULER-INSTALL-VERIFICATION"
    assert decision["status"] == "VERIFIED_SCHEDULER_INSTALL_ENABLE_NO_START"
    assert decision["verification_passed"] is True
    assert decision["scheduler_timer_enabled"] is True
    assert decision["scheduler_timer_started"] is False
    assert decision["scheduler_service_started"] is False
    assert decision["current_r5_pid"] == 23133
    assert decision["duplicate_r5"] is False
    assert decision["paper_ready_candidates"] == 0
    assert all(row["passed"] for row in payload["verification_checks"])
    assert payload["safety_flags"]["systemctl_mutating_commands_executed"] == 0
    assert payload["safety_flags"]["scheduler_timer_started"] is False
    assert payload["safety_flags"]["runs_refresh_jobs"] is False
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert artifacts.manifest_path.exists()


def test_phase3bb_r37_verifies_scheduler_timer_active_after_start(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    runner = _fake_probe_runner(
        {
            "scheduler_timer_systemd": (
                "\n".join(
                    [
                        "LoadState=loaded",
                        "UnitFileState=enabled",
                        "ActiveState=active",
                        "SubState=waiting",
                        "FragmentPath=/etc/systemd/system/kalshi-multicategory-refresh-scheduler.timer",
                        "ExecMainPID=0",
                        "",
                    ]
                ),
                True,
                0,
                "",
            ),
            "scheduler_timer_active": ("active\n", True, 0, ""),
            "scheduler_service_active": ("inactive\n", True, 0, ""),
            "scheduler_service_systemd": (
                "\n".join(
                    [
                        "LoadState=loaded",
                        "UnitFileState=static",
                        "ActiveState=inactive",
                        "SubState=dead",
                        "FragmentPath=/etc/systemd/system/kalshi-multicategory-refresh-scheduler.service",
                        "ExecMainPID=50324",
                        "",
                    ]
                ),
                True,
                0,
                "",
            ),
        }
    )

    with session_factory() as session:
        payload = build_phase3bb_r37_cloud_scheduler_install_verification(
            session,
            output_dir=reports_dir / "phase3bb_r37",
            reports_dir=reports_dir,
            probe_runner=runner,
        )

    decision = payload["verification_decision"]
    assert decision["status"] == "VERIFIED_SCHEDULER_INSTALL_TIMER_ACTIVE"
    assert decision["verification_passed"] is True
    assert decision["scheduler_timer_started"] is True
    assert decision["scheduler_service_started"] is False
    assert decision["ready_for_timer_start_handoff"] is False
    assert decision["ready_for_runtime_monitor"] is True
    assert decision["next_codex_step"] == "Phase 3BB-R40 - Cloud Scheduler Runtime Monitor"
    assert all(row["passed"] for row in payload["verification_checks"])


def test_phase3bb_r37_blocks_when_timer_missing(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    runner = _fake_probe_runner(
        {
            "scheduler_timer_unit_file": ("", False, 1, "missing timer"),
            "scheduler_timer_systemd": ("LoadState=not-found\n", True, 0, ""),
            "scheduler_timer_enabled": ("disabled\n", True, 0, ""),
        }
    )

    with session_factory() as session:
        payload = build_phase3bb_r37_cloud_scheduler_install_verification(
            session,
            output_dir=reports_dir / "phase3bb_r37",
            reports_dir=reports_dir,
            probe_runner=runner,
        )

    decision = payload["verification_decision"]
    assert decision["status"] == "BLOCKED_SCHEDULER_INSTALL_VERIFICATION"
    assert decision["verification_passed"] is False
    assert decision["first_failed_check"] == "scheduler_timer_unit_installed"
    assert "PHASE3BB_R36_EXECUTE=I_APPROVE_R36_SCHEDULER_INSTALL" in (
        decision["operator_next_command"]
    )


def test_phase3bb_r37_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r37-cloud-scheduler-install-verification", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r37-cloud-scheduler-install-verification" in result.output
    assert "--per-probe-timeout-seconds" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r37.db'}")
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

    r36_dir = reports_dir / "phase3bb_r36"
    r36_dir.mkdir(parents=True, exist_ok=True)
    (r36_dir / "cloud_scheduler_install_handoff.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "handoff_decision": {
                    "status": "HANDOFF_READY_SCHEDULER_INSTALL_ENABLE_NO_START",
                    "handoff_ready": True,
                    "r5_pid": 23133,
                    "watch_state": "WAITING_FOR_POSITIVE_EV",
                    "paper_ready_candidates": 0,
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_probe_runner(
    overrides: dict[str, tuple[str, bool, int | None, str]] | None = None,
):
    outputs = {
        "scheduler_service_unit_file": (
            "\n".join(
                [
                    "[Service]",
                    "Type=oneshot",
                    "ExecStart=/opt/kalshi-predictive-bot/scripts/"
                    "kalshi-multicategory-refresh-runner.sh",
                    "",
                ]
            ),
            True,
            0,
            "",
        ),
        "scheduler_timer_unit_file": (
            "\n".join(["[Timer]", "OnUnitActiveSec=15min", "", "[Install]", "WantedBy=timers.target", ""]),
            True,
            0,
            "",
        ),
        "scheduler_runner_script": (
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    ".venv/bin/kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5",
                    "writer_json=$(.venv/bin/kalshi-bot db-writer-monitor --json)",
                    "echo '[phase3bb-r35] Writer active; skip writer-gated job weather'",
                    ".venv/bin/kalshi-bot phase3bb-r33-cloud-paper-only-operations-readiness",
                    "",
                ]
            ),
            True,
            0,
            "",
        ),
        "scheduler_service_systemd": (
            "\n".join(
                [
                    "LoadState=loaded",
                    "UnitFileState=static",
                    "ActiveState=inactive",
                    "SubState=dead",
                    "FragmentPath=/etc/systemd/system/kalshi-multicategory-refresh-scheduler.service",
                    "ExecMainPID=0",
                    "",
                ]
            ),
            True,
            0,
            "",
        ),
        "scheduler_timer_systemd": (
            "\n".join(
                [
                    "LoadState=loaded",
                    "UnitFileState=enabled",
                    "ActiveState=inactive",
                    "SubState=dead",
                    "FragmentPath=/etc/systemd/system/kalshi-multicategory-refresh-scheduler.timer",
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
        "r5_status": (
            json.dumps(
                {
                    "pid": 23133,
                    "process": {
                        "phase3bc_r5_process_running": True,
                        "phase3bc_r5_pids": [23133],
                    },
                    "guard": {"status": "RUNNING", "should_stop": False},
                    "latest_watch_state": "WAITING_FOR_POSITIVE_EV",
                    "latest_summary": {
                        "positive_ev_rows": 4,
                        "paper_ready_candidates": 0,
                    },
                }
            ),
            True,
            0,
            "",
        ),
        "r5_guard_dry_run": (
            json.dumps({"after": {"guard": {"status": "RUNNING", "should_stop": False}}}),
            True,
            0,
            "",
        ),
        "db_writer_monitor": (
            json.dumps(
                {
                    "status": "WRITER_ACTIVE",
                    "safe_to_start_write": False,
                    "current_writer_pid": 23133,
                }
            ),
            True,
            0,
            "",
        ),
        "r5_processes": (
            "23133 /opt/kalshi-predictive-bot/.venv/bin/python -m kalshi_predictor "
            "phase3bc-r5-crypto-freshness-watch\n",
            True,
            0,
            "",
        ),
        "command_registry": ("COMMAND_REGISTRY_OK\n", True, 0, ""),
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
