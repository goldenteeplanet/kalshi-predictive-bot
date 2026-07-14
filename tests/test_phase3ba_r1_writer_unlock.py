from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_r1
from kalshi_predictor.cli import app


def _metadata() -> dict:
    return {
        "generated_at": "2026-07-10T20:00:00+00:00",
        "repository_root": "/tmp/repo",
        "git_branch": "main",
        "git_commit": "abc123",
        "git_dirty": "clean",
        "python_executable": "/tmp/python",
        "installed_package_path": "/tmp/phase3ba_r1.py",
        "resolved_database_url": "sqlite:///tmp/test.db",
        "database_fingerprint": {"kind": "sqlite_file_stat", "fingerprint": "db123"},
        "database_location": "/tmp/test.db",
        "migration_revision": "rev1",
        "timezone": "UTC",
        "command_arguments": {
            "command": "kalshi-bot phase3ba-r1-writer-unlock",
            "argv": ["phase3ba-r1-writer-unlock"],
        },
        "data_watermark": {"latest_snapshot_at": "2026-07-10T19:59:00+00:00"},
        "paper_only_safety": "PAPER_ONLY_NO_EXCHANGE_WRITES",
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "safety_flags": {
            "paper_only": True,
            "live_trading_enabled": False,
            "demo_exchange_writes_enabled": False,
            "submits_cancels_replaces_orders": False,
            "creates_paper_trades": False,
            "allowed_process_stop": "guarded_overrun_phase3bc_r5_only",
        },
    }


def test_phase3ba_r1_stops_overrun_r5_and_restarts_one_watcher(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ba_r1, "_metadata", lambda **_: _metadata())
    writer_states = iter(
        [
            {
                "status": "WRITER_ACTIVE",
                "safe_to_start_write": False,
                "current_writer_pid": 10041,
                "current_writer_command": (
                    "python -m kalshi_predictor.cli "
                    "phase3bc-r5-crypto-freshness-watch"
                ),
            },
            {"status": "CLEAR", "safe_to_start_write": True, "current_writer_pid": None},
            {"status": "CLEAR", "safe_to_start_write": True, "current_writer_pid": None},
        ]
    )
    monkeypatch.setattr(phase3ba_r1, "_monitor_writer", lambda _settings: next(writer_states))
    status_states = iter(
        [
            {
                "guard": {"status": "OVERRUNNING", "should_stop": True, "pid": 10041},
                "process": {"phase3bc_r5_pids": [10041]},
            },
            {
                "guard": {"status": "RUNNING", "should_stop": False, "pid": 22222},
                "process": {"phase3bc_r5_pids": [22222]},
            },
        ]
    )
    monkeypatch.setattr(
        phase3ba_r1,
        "build_phase3bc_r5_status",
        lambda *, output_dir: next(status_states),
    )
    commands: list[list[str]] = []

    def fake_run(command_args, *, timeout_seconds):
        commands.append(command_args)
        return {
            "status": "COMPLETED",
            "returncode": 0,
            "timeout_seconds": timeout_seconds,
            "stdout": "ok",
            "stderr": "",
        }

    monkeypatch.setattr(phase3ba_r1, "_run_registered_command", fake_run)

    artifacts = phase3ba_r1.write_phase3ba_r1_writer_unlock_report(
        output_dir=Path("reports/phase3ba_r1"),
        reports_dir=Path("reports"),
        settings=object(),
        command_args=["phase3ba-r1-writer-unlock"],
        post_stop_wait_seconds=0,
    )
    payload = json.loads(artifacts.writer_unlock_path.read_text(encoding="utf-8"))

    assert payload["status"] == "RESTARTED_ONE_R5_WATCHER"
    assert payload["summary"]["old_writer_pid_cleared"] is True
    assert payload["summary"]["exactly_one_r5_watcher_running"] is True
    assert payload["summary"]["running_r5_pids_after_restart"] == [22222]
    assert commands[0][0] == "phase3bc-r5-unattended-guard"
    assert "--stop-overrun" in commands[0]
    assert commands[1][0] == "phase3bc-r5-unattended-start"
    assert artifacts.executive_summary_path.exists()
    assert artifacts.r5_restart_status_path.exists()
    assert artifacts.next_actions_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3ba_r1_refuses_non_r5_active_writer(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ba_r1, "_metadata", lambda **_: _metadata())
    monkeypatch.setattr(
        phase3ba_r1,
        "_monitor_writer",
        lambda _settings: {
            "status": "WRITER_ACTIVE",
            "safe_to_start_write": False,
            "current_writer_pid": 5151,
            "current_writer_command": "python -m kalshi_predictor.cli sync-markets",
        },
    )
    monkeypatch.setattr(
        phase3ba_r1,
        "build_phase3bc_r5_status",
        lambda *, output_dir: {
            "guard": {"status": "OVERRUNNING", "should_stop": True, "pid": 10041},
            "process": {"phase3bc_r5_pids": [10041]},
        },
    )

    def fail_run(*args, **kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("guard/start command should not run")

    monkeypatch.setattr(phase3ba_r1, "_run_registered_command", fail_run)

    artifacts = phase3ba_r1.write_phase3ba_r1_writer_unlock_report(
        output_dir=Path("reports/phase3ba_r1"),
        reports_dir=Path("reports"),
        settings=object(),
        command_args=["phase3ba-r1-writer-unlock"],
    )
    payload = json.loads(artifacts.writer_unlock_path.read_text(encoding="utf-8"))

    assert payload["status"] == "BLOCKED_WRITER_NOT_MATCHING_R5"
    assert payload["preflight_validations"]["ok"] is False
    assert payload["next_action"]["command"] == "kalshi-bot db-writer-monitor --json"


def test_phase3ba_r1_stops_overrun_r5_even_after_writer_lane_clears(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ba_r1, "_metadata", lambda **_: _metadata())
    writer_states = iter(
        [
            {"status": "OPEN_READERS", "safe_to_start_write": True, "current_writer_pid": None},
            {"status": "OPEN_READERS", "safe_to_start_write": True, "current_writer_pid": None},
            {"status": "OPEN_READERS", "safe_to_start_write": True, "current_writer_pid": None},
        ]
    )
    monkeypatch.setattr(phase3ba_r1, "_monitor_writer", lambda _settings: next(writer_states))
    status_states = iter(
        [
            {
                "guard": {"status": "OVERRUNNING", "should_stop": True, "pid": 10041},
                "process": {"phase3bc_r5_pids": [10041]},
            },
            {
                "guard": {"status": "RUNNING", "should_stop": False, "pid": 33333},
                "process": {"phase3bc_r5_pids": [33333]},
            },
        ]
    )
    monkeypatch.setattr(
        phase3ba_r1,
        "build_phase3bc_r5_status",
        lambda *, output_dir: next(status_states),
    )
    commands: list[list[str]] = []

    def fake_run(command_args, *, timeout_seconds):
        commands.append(command_args)
        return {
            "status": "COMPLETED",
            "returncode": 0,
            "timeout_seconds": timeout_seconds,
            "stdout": "ok",
            "stderr": "",
        }

    monkeypatch.setattr(phase3ba_r1, "_run_registered_command", fake_run)

    artifacts = phase3ba_r1.write_phase3ba_r1_writer_unlock_report(
        output_dir=Path("reports/phase3ba_r1"),
        reports_dir=Path("reports"),
        settings=object(),
        command_args=["phase3ba-r1-writer-unlock"],
        post_stop_wait_seconds=0,
    )
    payload = json.loads(artifacts.writer_unlock_path.read_text(encoding="utf-8"))

    assert payload["preflight_validations"]["status"] == (
        "PRECONDITIONS_MET_NO_ACTIVE_WRITER_R5_OVERRUNNING"
    )
    assert payload["status"] == "RESTARTED_ONE_R5_WATCHER"
    assert payload["summary"]["target_stop_pid"] == 10041
    assert payload["summary"]["target_r5_pid_cleared_after_restart"] is True
    assert payload["summary"]["running_r5_pids_after_restart"] == [33333]
    assert commands[0][0] == "phase3bc-r5-unattended-guard"
    assert commands[1][0] == "phase3bc-r5-unattended-start"


def test_phase3ba_r1_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-r1-writer-unlock", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-r1-writer-unlock" in result.output
