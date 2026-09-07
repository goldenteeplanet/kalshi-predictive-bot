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
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import (
    build_phase3bb_r18_cloud_scheduler_runtime_cutover,
    write_phase3bb_r18_cloud_scheduler_runtime_cutover_report,
)


def test_phase3bb_r18_waits_while_manual_r5_is_healthy(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=10573)

    with session_factory() as session:
        artifacts = write_phase3bb_r18_cloud_scheduler_runtime_cutover_report(
            session,
            output_dir=reports_dir / "phase3bb_r18",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(pid=10573),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["phase"] == "3BB-R18-CLOUD-SCHEDULER-RUNTIME-CUTOVER"
    decision = payload["runtime_cutover_decision"]
    assert decision["status"] == "WAIT_FOR_MANUAL_R5_TO_EXIT"
    assert decision["recommended_action"] == "WAIT"
    assert decision["service_enabled"] is True
    assert decision["service_started"] is False
    assert decision["current_r5_pid"] == 10573
    assert decision["duplicate_r5"] is False
    assert decision["codex_executed_start"] is False
    assert payload["safety_flags"]["starts_service"] is False
    assert all(row["passed"] for row in payload["runtime_cutover_checks"])
    assert artifacts.manifest_path.exists()


def test_phase3bb_r18_clear_writer_is_not_reported_as_matching_r5(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=10573)

    with session_factory() as session:
        payload = build_phase3bb_r18_cloud_scheduler_runtime_cutover(
            session,
            output_dir=reports_dir / "phase3bb_r18",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(pid=10573, writer_active=False),
        )

    parsed = payload["parsed_remote_state"]
    decision = payload["runtime_cutover_decision"]
    assert decision["status"] == "WAIT_FOR_MANUAL_R5_TO_EXIT"
    assert parsed["writer_pid"] is None
    assert parsed["writer_clear_or_matches_r5"] is True
    assert parsed["writer_matches_r5"] is False
    assert decision["writer_matches_r5"] is False
    assert all(row["passed"] for row in payload["runtime_cutover_checks"])


def test_phase3bb_r18_ready_to_start_service_after_manual_r5_exits(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=10573)

    with session_factory() as session:
        payload = build_phase3bb_r18_cloud_scheduler_runtime_cutover(
            session,
            output_dir=reports_dir / "phase3bb_r18",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(pid=None),
        )

    decision = payload["runtime_cutover_decision"]
    assert decision["status"] == "READY_FOR_SYSTEMD_START"
    assert decision["recommended_action"] == "START_SERVICE_AFTER_R5_EXIT"
    assert "systemctl start kalshi-r5-watcher.service" in decision["operator_next_command"]
    assert decision["codex_executed_start"] is False


def test_phase3bb_r18_confirms_systemd_owned_r5(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=10573)

    with session_factory() as session:
        payload = build_phase3bb_r18_cloud_scheduler_runtime_cutover(
            session,
            output_dir=reports_dir / "phase3bb_r18",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(pid=10573, service_active=True),
        )

    decision = payload["runtime_cutover_decision"]
    assert decision["status"] == "SYSTEMD_OWNS_R5"
    assert decision["recommended_action"] == "MONITOR_SYSTEMD_R5"
    assert decision["service_started"] is True
    assert decision["service_owns_r5"] is True


def test_phase3bb_r18_prefers_live_process_scan_over_stale_status_pid(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=10573)

    with session_factory() as session:
        payload = build_phase3bb_r18_cloud_scheduler_runtime_cutover(
            session,
            output_dir=reports_dir / "phase3bb_r18",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(pid=16798, service_active=True, status_pid=10573),
        )

    parsed = payload["parsed_remote_state"]
    decision = payload["runtime_cutover_decision"]
    assert parsed["r5_status_pid"] == 10573
    assert parsed["r5_status_pid_stale"] is True
    assert parsed["r5_pid"] == 16798
    assert parsed["service_exec_main_pid"] == 16798
    assert parsed["writer_pid"] == 16798
    assert decision["status"] == "SYSTEMD_OWNS_R5"
    assert decision["service_owns_r5"] is True


def test_phase3bb_r18_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r18-cloud-scheduler-runtime-cutover", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r18-cloud-scheduler-runtime-cutover" in result.output
    assert "--service-name" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r18.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path, *, pid: int) -> None:
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
                    "current_r5_pid": pid,
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
    r17_dir = reports_dir / "phase3bb_r17"
    r17_dir.mkdir(parents=True, exist_ok=True)
    (r17_dir / "cloud_service_install_verification.json").write_text(
        json.dumps(
            {
                "cloud_target": cloud_target,
                "verification_decision": {
                    "status": "VERIFIED_ENABLE_NO_START_HANDOFF",
                    "verification_passed": True,
                    "current_r5_pid": pid,
                    "service_enabled": True,
                    "service_started": False,
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_runner(
    *,
    pid: int | None,
    service_active: bool = False,
    writer_active: bool | None = None,
    status_pid: int | None = None,
):
    if writer_active is None:
        writer_active = pid is not None
    if status_pid is None:
        status_pid = pid
    service_pid = pid if service_active and pid is not None else 0
    r5_status = {
        "pid": status_pid,
        "process": {
            "phase3bc_r5_process_running": pid is not None,
            "phase3bc_r5_pids": [] if pid is None else [pid],
            "status": "RUNNING" if pid is not None else "STOPPED",
        },
        "guard": {
            "status": "RUNNING" if pid is not None else "STOPPED_WITH_STALE_PID",
            "should_stop": False,
            "recommended_next_action": "Crypto watch state is acceptable.",
        },
        "latest_watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
        "latest_summary": {
            "paper_ready_candidates": 0,
            "positive_ev_rows": 4 if pid is not None else 0,
        },
    }
    guard = {
        "before": r5_status,
        "after": r5_status,
        "action": {"requested_stop_overrun": False, "terminated_pid": None},
        "status": r5_status["guard"]["status"],
    }
    writer = {
        "status": "WRITER_ACTIVE" if writer_active else "CLEAR",
        "safe_to_start_write": not writer_active,
        "current_writer_pid": pid if writer_active else None,
    }
    active_state = "active" if service_active else "inactive"
    process_line = (
        ""
        if pid is None
        else (
            f"{pid} /opt/kalshi-predictive-bot/.venv/bin/python "
            "-m kalshi_predictor.cli phase3bc-r5-crypto-freshness-watch\n"
        )
    )
    outputs = {
        "systemd_unit": "\n".join(
            [
                "LoadState=loaded",
                "UnitFileState=enabled",
                f"ActiveState={active_state}",
                "SubState=running" if service_active else "SubState=dead",
                "FragmentPath=/etc/systemd/system/kalshi-r5-watcher.service",
                f"ExecMainPID={service_pid}",
            ]
        ),
        "systemd_enabled": "enabled\n",
        "systemd_active": f"{active_state}\n",
        "r5_status": json.dumps(r5_status),
        "r5_guard_dry_run": json.dumps(guard),
        "db_writer_monitor": json.dumps(writer),
        "r5_processes": process_line,
        "r5_pid_file": "" if pid is None else f"{pid}\n",
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
