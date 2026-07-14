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
from kalshi_predictor.phase3bb_r17_cloud_service_install_verification import (
    build_phase3bb_r17_cloud_service_install_verification,
    write_phase3bb_r17_cloud_service_install_verification_report,
)


def test_phase3bb_r17_verifies_enable_no_start_handoff(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r17_cloud_service_install_verification_report(
            session,
            output_dir=reports_dir / "phase3bb_r17",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(pid=1917),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["phase"] == "3BB-R17-CLOUD-SERVICE-INSTALL-VERIFICATION"
    assert payload["verification_decision"]["status"] == "VERIFIED_ENABLE_NO_START_HANDOFF"
    assert payload["verification_decision"]["verification_passed"] is True
    assert payload["verification_decision"]["service_enabled"] is True
    assert payload["verification_decision"]["service_started"] is False
    assert payload["verification_decision"]["current_r5_pid"] == 1917
    assert payload["safety_flags"]["starts_r5_watcher"] is False
    assert payload["safety_flags"]["stops_processes"] is False
    assert payload["safety_flags"]["systemctl_mutating_commands_executed"] == 0
    assert all(row["passed"] for row in payload["verification_checks"])
    assert artifacts.manifest_path.exists()


def test_phase3bb_r17_blocks_if_service_started_unexpectedly(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r17_cloud_service_install_verification(
            session,
            output_dir=reports_dir / "phase3bb_r17",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(pid=1917, service_active=True),
        )

    assert payload["verification_decision"]["status"] == (
        "BLOCKED_SERVICE_INSTALL_VERIFICATION"
    )
    assert payload["verification_decision"]["verification_passed"] is False
    assert payload["verification_decision"]["first_failed_check"] == "service_not_started_now"
    assert payload["verification_decision"]["service_started"] is True


def test_phase3bb_r17_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r17-cloud-service-install-verification", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r17-cloud-service-install-verification" in result.output
    assert "--service-name" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r17.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
    generated_at = datetime.now(UTC).isoformat()
    cloud_target = {
        "ssh_target": "kalshi@203.0.113.10",
        "identity_file": "~/.ssh/id_ed25519_do",
        "app_path": "/opt/kalshi-predictive-bot",
        "env_path": "/etc/kalshi-bot/kalshi-bot.env",
        "db_path": "/var/lib/kalshi-bot/kalshi_phase1.db",
        "reports_path": "/opt/kalshi-predictive-bot/reports",
    }
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
    r13_dir = reports_dir / "phase3bb_r13"
    r13_dir.mkdir(parents=True, exist_ok=True)
    (r13_dir / "cloud_scheduler_adoption.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "cloud_target": cloud_target,
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
                "cloud_target": cloud_target,
                "service_plan": {
                    "status": "DRAFT_READY_FOR_REVIEW",
                    "existing_r5_pid": 1917,
                    "r13_recommendation": "ADOPT_EXISTING_R5",
                    "service_name": "kalshi-r5-watcher.service",
                    "guard_script_path": (
                        "/opt/kalshi-predictive-bot/scripts/cloud/"
                        "kalshi-r5-start-guard.sh"
                    ),
                },
            }
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
    r16_dir = reports_dir / "phase3bb_r16"
    r16_dir.mkdir(parents=True, exist_ok=True)
    (r16_dir / "cloud_service_install_handoff.json").write_text(
        json.dumps(
            {
                "cloud_target": cloud_target,
                "handoff_decision": {
                    "status": "HANDOFF_READY_ENABLE_NO_START",
                    "handoff_ready": True,
                    "current_r5_pid": 1917,
                    "codex_executed_install": False,
                    "codex_executed_enable": False,
                    "codex_executed_start": False,
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_runner(*, pid: int, service_active: bool = False):
    service_text = "\n".join(
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
    )
    guard_text = "\n".join(
        [
            "#!/usr/bin/env bash",
            "existing_pids=$(pgrep -f 'phase3bc-r5-crypto-freshness-watch' || true)",
            "echo 'Refusing duplicate R5 start'",
            ".venv/bin/kalshi-bot db-writer-monitor --json",
            "",
        ]
    )
    r5_status = {
        "pid": pid,
        "process": {
            "phase3bc_r5_process_running": True,
            "phase3bc_r5_pids": [pid],
            "status": "RUNNING",
        },
        "guard": {
            "status": "RUNNING",
            "should_stop": False,
            "recommended_next_action": "Crypto watch is running inside its timeout budget.",
        },
        "latest_watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
        "latest_summary": {
            "paper_ready_candidates": 0,
            "positive_ev_rows": 4,
            "liquidity_actionability_state": "POSITIVE_EV_NO_EXECUTABLE_BOOK",
        },
    }
    guard = {
        "before": r5_status,
        "after": r5_status,
        "action": {"requested_stop_overrun": False, "terminated_pid": None},
        "status": "RUNNING",
    }
    writer = {
        "status": "WRITER_ACTIVE",
        "safe_to_start_write": False,
        "current_writer_pid": pid,
    }
    active_state = "active" if service_active else "inactive"
    exec_main_pid = "3000" if service_active else "0"
    outputs = {
        "service_unit_file": service_text,
        "guard_script": guard_text,
        "systemd_unit": "\n".join(
            [
                "LoadState=loaded",
                "UnitFileState=enabled",
                f"ActiveState={active_state}",
                "SubState=running" if service_active else "SubState=dead",
                "FragmentPath=/etc/systemd/system/kalshi-r5-watcher.service",
                f"ExecMainPID={exec_main_pid}",
            ]
        ),
        "systemd_enabled": "enabled\n",
        "systemd_active": f"{active_state}\n",
        "r5_status": json.dumps(r5_status),
        "r5_guard_dry_run": json.dumps(guard),
        "db_writer_monitor": json.dumps(writer),
        "r5_processes": (
            f"{pid} /opt/kalshi-predictive-bot/.venv/bin/python "
            "-m kalshi_predictor.cli phase3bc-r5-crypto-freshness-watch\n"
        ),
        "r5_pid_file": f"{pid}\n",
    }

    def runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        return RemoteProbeResult(
            name=probe.name,
            command=probe.command,
            ok=True,
            exit_code=0,
            stdout=outputs[probe.name],
            stderr="",
            duration_seconds=0.01,
        )

    return runner
