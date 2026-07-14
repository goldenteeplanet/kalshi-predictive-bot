from __future__ import annotations

import json
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
from kalshi_predictor.phase3bb_r13_cloud_scheduler_adoption import (
    build_phase3bb_r13_cloud_scheduler_adoption,
    write_phase3bb_r13_cloud_scheduler_adoption_report,
)


def test_phase3bb_r13_adopts_healthy_existing_r5(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=1917)

    with session_factory() as session:
        artifacts = write_phase3bb_r13_cloud_scheduler_adoption_report(
            session,
            output_dir=reports_dir / "phase3bb_r13",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(pid=1917),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["phase"] == "3BB-R13-CLOUD-SCHEDULER-ADOPTION-DRY-RUN"
    assert payload["adoption_decision"]["recommendation"] == "ADOPT_EXISTING_R5"
    assert payload["adoption_decision"]["current_r5_pid"] == 1917
    assert payload["safety_flags"]["starts_r5_watcher"] is False
    assert payload["safety_flags"]["stops_processes"] is False
    assert artifacts.operator_command_path.exists()
    assert "Phase 3BB-R14" in artifacts.next_actions_path.read_text(
        encoding="utf-8"
    )
    assert "phase3bb-r13-cloud-scheduler-adoption" in payload["next_operator_command"]


def test_phase3bb_r13_recommends_guarded_stop_for_overrun(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=1917)

    with session_factory() as session:
        payload = build_phase3bb_r13_cloud_scheduler_adoption(
            session,
            output_dir=reports_dir / "phase3bb_r13",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(pid=1917, guard_status="OVERRUNNING", should_stop=True),
        )

    assert payload["adoption_decision"]["recommendation"] == "STOP_OVERRUN_R5"
    assert "--stop-overrun" in payload["adoption_decision"]["operator_next_command"]
    assert payload["safety_flags"]["guarded_stop_command_written_only"] is True
    assert payload["safety_flags"]["stops_processes"] is False


def test_phase3bb_r13_waits_on_duplicate_r5(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, pid=1917)

    with session_factory() as session:
        payload = build_phase3bb_r13_cloud_scheduler_adoption(
            session,
            output_dir=reports_dir / "phase3bb_r13",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(pid=1917, extra_pids=[2222]),
        )

    assert payload["adoption_decision"]["recommendation"] == "WAIT"
    assert payload["adoption_decision"]["duplicate_r5"] is True


def test_phase3bb_r13_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r13-cloud-scheduler-adoption", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r13-cloud-scheduler-adoption" in result.output
    assert "--expected-r5-pid" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r13.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path, *, pid: int) -> None:
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
    r12_dir = reports_dir / "phase3bb_r12"
    r12_dir.mkdir(parents=True, exist_ok=True)
    (r12_dir / "cloud_bootstrap_verification.json").write_text(
        json.dumps({"parsed_remote_state": {"r5_pids": [pid]}}),
        encoding="utf-8",
    )


def _fake_runner(
    *,
    pid: int,
    guard_status: str = "RUNNING",
    should_stop: bool = False,
    extra_pids: list[int] | None = None,
):
    pids = [pid, *(extra_pids or [])]
    r5_status = {
        "pid": pid,
        "process": {
            "phase3bc_r5_process_running": True,
            "phase3bc_r5_pids": pids,
            "status": "RUNNING",
        },
        "guard": {
            "status": guard_status,
            "should_stop": should_stop,
            "recommended_next_action": "Crypto watch is running inside its timeout budget.",
            "latest_age_seconds": 120,
            "seconds_until_timeout": 3600,
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
        "status": guard_status,
    }
    writer = {
        "status": "WRITER_ACTIVE",
        "safe_to_start_write": False,
        "current_writer_pid": pid,
    }
    outputs = {
        "r5_status": json.dumps(r5_status),
        "r5_guard_dry_run": json.dumps(guard),
        "db_writer_monitor": json.dumps(writer),
        "r5_pid_file": str(pid),
        "expected_pid_process": f"{pid} 1234 python -m kalshi_predictor.cli phase3bc-r5",
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
