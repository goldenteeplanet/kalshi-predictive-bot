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
from kalshi_predictor.phase3bb_r19_cloud_systemd_cutover import (
    APPROVAL_TOKEN,
    build_phase3bb_r19_cloud_systemd_cutover,
    write_phase3bb_r19_cloud_systemd_cutover_report,
)


def test_phase3bb_r19_dry_run_ready_for_operator_cutover(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=10573)

    with session_factory() as session:
        artifacts = write_phase3bb_r19_cloud_systemd_cutover_report(
            session,
            output_dir=reports_dir / "phase3bb_r19",
            reports_dir=reports_dir,
            probe_runner=_stateful_runner(pid=10573),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["cutover_decision"]
    assert decision["status"] == "READY_FOR_OPERATOR_APPROVED_CUTOVER"
    assert decision["recommended_action"] == "RUN_APPROVED_CUTOVER"
    assert decision["codex_executed_sigterm"] is False
    assert decision["codex_executed_systemd_start"] is False
    assert payload["remote_cutover_results"] == []
    assert payload["control_target"]["ssh_target"] == "root@203.0.113.10"
    assert artifacts.manifest_path.exists()


def test_phase3bb_r19_execute_requires_approval_token(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=10573)

    with session_factory() as session:
        payload = build_phase3bb_r19_cloud_systemd_cutover(
            session,
            output_dir=reports_dir / "phase3bb_r19",
            reports_dir=reports_dir,
            execute=True,
            probe_runner=_stateful_runner(pid=10573),
        )

    decision = payload["cutover_decision"]
    assert decision["status"] == "BLOCKED_APPROVAL_TOKEN_REQUIRED"
    assert decision["recommended_action"] == "ADD_APPROVAL_TOKEN"
    assert payload["remote_cutover_results"] == []


def test_phase3bb_r19_approved_cutover_starts_systemd_owner(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=10573)

    with session_factory() as session:
        payload = build_phase3bb_r19_cloud_systemd_cutover(
            session,
            output_dir=reports_dir / "phase3bb_r19",
            reports_dir=reports_dir,
            execute=True,
            approval_token=APPROVAL_TOKEN,
            probe_runner=_stateful_runner(pid=10573, service_pid_after_start=22001),
        )

    decision = payload["cutover_decision"]
    assert decision["status"] == "CUTOVER_COMPLETE_SYSTEMD_OWNS_R5"
    assert decision["recommended_action"] == "VERIFY_AND_MONITOR"
    assert decision["codex_executed_sigterm"] is True
    assert decision["codex_executed_systemd_start"] is True
    assert payload["post_cutover_r18"]["runtime_cutover_decision"]["status"] == "SYSTEMD_OWNS_R5"
    assert [row["name"] for row in payload["remote_cutover_results"]] == [
        "control_identity",
        "manual_r5_graceful_sigterm",
        "verify_manual_r5_exited",
        "systemd_start",
        "systemd_show_after_start",
    ]


def test_phase3bb_r19_reports_already_complete_when_systemd_owns_r5(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=10573)

    with session_factory() as session:
        payload = build_phase3bb_r19_cloud_systemd_cutover(
            session,
            output_dir=reports_dir / "phase3bb_r19",
            reports_dir=reports_dir,
            probe_runner=_stateful_runner(pid=22001, service_active=True),
        )

    decision = payload["cutover_decision"]
    assert decision["status"] == "CUTOVER_ALREADY_COMPLETE_SYSTEMD_OWNS_R5"
    assert decision["recommended_action"] == "MONITOR_SYSTEMD_R5"
    assert decision["codex_executed_sigterm"] is False
    assert decision["codex_executed_systemd_start"] is False
    assert payload["remote_cutover_results"] == []


def test_phase3bb_r19_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r19-cloud-systemd-cutover", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r19-cloud-systemd-cutover" in result.output
    assert "--execute" in result.output
    assert "--control-ssh-target" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r19.db'}")
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


def _stateful_runner(
    *,
    pid: int | None,
    service_pid_after_start: int = 22001,
    service_active: bool = False,
):
    state = {"pid": pid, "service_active": service_active}

    def runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        if probe.name == "manual_r5_graceful_sigterm":
            state["pid"] = None
            return _result(probe, "SIGTERM_EXITED\n")
        if probe.name == "verify_manual_r5_exited":
            return _result(probe, "PID_EXITED\n")
        if probe.name == "systemd_start":
            state["pid"] = service_pid_after_start
            state["service_active"] = True
            return _result(probe, "")
        if probe.name == "control_identity":
            return _result(probe, "root\nkalshi-bot-01\n")
        outputs = _probe_outputs(
            pid=state["pid"],
            service_active=bool(state["service_active"]),
        )
        return _result(probe, outputs.get(probe.name, ""))

    return runner


def _probe_outputs(*, pid: int | None, service_active: bool) -> dict[str, str]:
    service_pid = pid if service_active and pid is not None else 0
    r5_status = {
        "pid": pid,
        "process": {
            "phase3bc_r5_process_running": pid is not None,
            "phase3bc_r5_pids": [] if pid is None else [pid],
            "status": "RUNNING" if pid is not None else "STOPPED",
        },
        "guard": {
            "status": "RUNNING" if pid is not None else "STOPPED_WITH_STALE_PID",
            "should_stop": False,
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
        "status": "WRITER_ACTIVE" if pid is not None and not service_active else "CLEAR",
        "safe_to_start_write": pid is None or service_active,
        "current_writer_pid": pid if pid is not None and not service_active else None,
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
    return {
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
        "systemd_show_after_start": "\n".join(
            [
                "LoadState=loaded",
                "UnitFileState=enabled",
                f"ActiveState={active_state}",
                "SubState=running" if service_active else "SubState=dead",
                "FragmentPath=/etc/systemd/system/kalshi-r5-watcher.service",
                f"ExecMainPID={service_pid}",
            ]
        ),
    }


def _result(probe: RemoteProbe, stdout: str, *, ok: bool = True) -> RemoteProbeResult:
    return RemoteProbeResult(
        name=probe.name,
        command=probe.command,
        ok=ok,
        exit_code=0 if ok else 1,
        stdout=stdout,
        stderr="",
        duration_seconds=0.01,
    )
