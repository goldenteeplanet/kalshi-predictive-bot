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
from kalshi_predictor.phase3bb_r46_cloud_scheduler_weather_writer_gate_repair import (
    build_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair,
    patch_runner_midrun_writer_gate,
    write_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair_report,
)


def test_patch_runner_adds_midrun_writer_busy_clean_skip() -> None:
    patched = patch_runner_midrun_writer_gate(_old_runner())

    assert "Writer became active during" in patched
    assert "Status: BUSY_WRITER|Database is busy" in patched
    assert "output=$(\"$@\" 2>&1)" in patched
    assert patched == patch_runner_midrun_writer_gate(patched)


def test_phase3bb_r46_ready_to_apply_when_runner_lacks_midrun_gate(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    _write_r45(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair_report(
            session,
            output_dir=reports_dir / "phase3bb_r46",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["repair_decision"]
    assert payload["phase"] == "3BB-R46-CLOUD-SCHEDULER-WEATHER-WRITER-GATE-REPAIR"
    assert decision["status"] == "READY_TO_APPLY_SCHEDULER_WRITER_GATE_REPAIR"
    assert decision["runner_patch_required"] is True
    assert decision["service_result_before"] == "exit-code"
    assert payload["parsed_repair_state"]["journal_busy_writer_seen"] is True
    assert all(row["passed"] for row in payload["repair_checks"])
    assert artifacts.runner_patch_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3bb_r46_apply_installs_runner_patch_and_resets_failed(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    _write_r45(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair(
            session,
            output_dir=reports_dir / "phase3bb_r46",
            reports_dir=reports_dir,
            apply=True,
            backup_first=True,
            reset_failed=True,
            probe_runner=_fake_probe_runner(post_apply=True),
        )

    decision = payload["repair_decision"]
    assert decision["status"] == "SCHEDULER_WRITER_GATE_REPAIR_INSTALLED"
    assert decision["runner_repaired_after"] is True
    assert decision["service_result_after"] == "success"
    assert payload["install_result"]["attempted"] is True
    assert payload["install_result"]["ok"] is True
    assert payload["safety_flags"]["scheduler_runner_written_to_system"] is True
    assert payload["safety_flags"]["scheduler_timer_started"] is False
    assert payload["safety_flags"]["creates_paper_trades"] is False


def test_phase3bb_r46_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r46-cloud-scheduler-weather-writer-gate-repair", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r46-cloud-scheduler-weather-writer-gate-repair" in result.output
    assert "--reset-failed" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r46.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
    r11_dir = reports_dir / "phase3bb_r11"
    r11_dir.mkdir(parents=True, exist_ok=True)
    (r11_dir / "codex_cloud_context.json").write_text(
        json.dumps(
            {
                "ssh_profile": {
                    "host": "203.0.113.10",
                    "user": "root",
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


def _write_r45(reports_dir: Path) -> None:
    r45_dir = reports_dir / "phase3bb_r45"
    r45_dir.mkdir(parents=True, exist_ok=True)
    (r45_dir / "weather_freshness_to_ranking_impact.json").write_text(
        json.dumps(
            {
                "impact_decision": {
                    "status": "BLOCKED_SCHEDULER_WRITER_GATE_FAILURE",
                    "first_weather_blocker": "SCHEDULER_BUSY_WRITER_EXIT_75",
                }
            }
        ),
        encoding="utf-8",
    )


def _fake_probe_runner(*, post_apply: bool = False):
    old_runner = _old_runner()
    patched_runner = patch_runner_midrun_writer_gate(old_runner)
    call_counts: dict[str, int] = {}

    def _runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        call_counts[probe.name] = call_counts.get(probe.name, 0) + 1
        if probe.name == "runner_script":
            stdout = patched_runner if post_apply and call_counts[probe.name] > 1 else old_runner
        elif probe.name == "scheduler_service_systemd":
            result = "success" if post_apply and call_counts[probe.name] > 1 else "exit-code"
            stdout = f"LoadState=loaded\nActiveState=failed\nSubState=failed\nResult={result}\nNRestarts=0\n"
        elif probe.name == "scheduler_timer_systemd":
            stdout = "LoadState=loaded\nActiveState=active\nSubState=waiting\nUnitFileState=enabled\n"
        elif probe.name == "scheduler_journal_tail":
            stdout = "Database is busy. Another bot process is using SQLite.\nStatus: BUSY_WRITER\n"
        elif probe.name == "db_writer_monitor_raw":
            stdout = json.dumps({"status": "WRITER_ACTIVE", "safe_to_start_write": False, "current_writer_pid": 59223})
        elif probe.name == "command_registry":
            stdout = "COMMAND_REGISTRY_OK\n"
        elif probe.name == "install_runner_writer_gate_repair":
            stdout = "INSTALLED_R46_SCHEDULER_WRITER_GATE_REPAIR\nreset_failed_exit=0\n"
        elif probe.name == "remote_time_utc":
            stdout = "2026-07-13T17:30:00Z\n"
        else:
            stdout = ""
        return RemoteProbeResult(
            name=probe.name,
            command=probe.command,
            ok=True,
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_seconds=0.01,
        )

    return _runner


def _old_runner() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

cd /opt/kalshi-predictive-bot
KALSHI_BOT=${KALSHI_BOT:-.venv/bin/kalshi-bot}

writer_clear() {
  "${KALSHI_BOT}" db-writer-monitor --json >/dev/null
}

run_job() {
  local job_id="$1"
  local writer_capable="$2"
  shift 2
  if [[ "${writer_capable}" == "true" ]] && ! writer_clear; then
    echo "[phase3bb-r35] Writer active; skip writer-gated job ${job_id}"
    return 0
  fi
  echo "[phase3bb-r35] running ${job_id}"
  "$@"
}

# cadence_minutes=30 category=weather-catalog
run_job weather_current_catalog_refresh true bash -lc 'set -euo pipefail; .venv/bin/kalshi-bot sync-markets --status open --limit 100 --max-pages 3 --series-ticker KXTEMPNYCH; .venv/bin/kalshi-bot market-legs-parse --refresh --limit 1500; .venv/bin/kalshi-bot phase3az-r12-weather-activation-preview --output-dir reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 --match-tolerance-hours 3'

# cadence_minutes=30 category=weather
run_job weather_fast_lane true .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane --output-dir reports/phase3bb_r2 --reports-dir reports
"""
