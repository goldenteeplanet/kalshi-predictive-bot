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
from kalshi_predictor.phase3bb_r40_cloud_scheduler_runtime_monitor import (
    build_phase3bb_r40_cloud_scheduler_runtime_monitor,
    write_phase3bb_r40_cloud_scheduler_runtime_monitor_report,
)


def test_phase3bb_r40_reports_overnight_ready_with_writer_gate_warning(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r40_cloud_scheduler_runtime_monitor_report(
            session,
            output_dir=reports_dir / "phase3bb_r40",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["runtime_decision"]
    assert payload["phase"] == "3BB-R40-CLOUD-SCHEDULER-RUNTIME-MONITOR"
    assert decision["status"] == "OVERNIGHT_READY_WITH_WRITER_GATE_WARNINGS"
    assert decision["overnight_safe_to_leave_running"] is True
    assert decision["will_create_paper_trades"] is False
    assert decision["will_submit_live_or_demo_orders"] is False
    assert decision["writer_gate_skip_count"] == 2
    assert decision["weather_catalog_hook_job_run_count"] == 1
    assert decision["weather_fast_lane_job_run_count"] == 1
    assert decision["weather_catalog_runtime_order_ok"] is True
    assert payload["parsed_runtime_state"]["weather_source_ingest_event_count"] == 1
    assert payload["parsed_runtime_state"]["weather_feature_build_event_count"] == 1
    assert payload["parsed_runtime_state"]["scheduler_traceback_count"] == 0
    assert payload["parsed_runtime_state"]["scheduler_runner_has_weather_catalog_hook"] is True
    assert payload["parsed_runtime_state"]["tailscale_private_access_ok"] is True
    assert all(row["passed"] for row in payload["runtime_checks"])
    assert artifacts.writer_gate_csv_path.exists()
    assert artifacts.job_csv_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3bb_r40_blocks_when_timer_inactive(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    runner = _fake_probe_runner({"scheduler_timer_active": ("inactive\n", True, 0, "")})

    with session_factory() as session:
        payload = build_phase3bb_r40_cloud_scheduler_runtime_monitor(
            session,
            output_dir=reports_dir / "phase3bb_r40",
            reports_dir=reports_dir,
            probe_runner=runner,
        )

    decision = payload["runtime_decision"]
    assert decision["status"] == "BLOCKED_CLOUD_RUNTIME_MONITOR"
    assert decision["overnight_safe_to_leave_running"] is False
    assert decision["first_failed_check"] == "scheduler_timer_active"


def test_phase3bb_r40_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r40-cloud-scheduler-runtime-monitor", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r40-cloud-scheduler-runtime-monitor" in result.output
    assert "--journal-lines" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r40.db'}")
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


def _fake_probe_runner(
    overrides: dict[str, tuple[str, bool, int | None, str]] | None = None,
):
    journal = "\n".join(
        [
            "Jul 13 04:37:03 kalshi-bot-01 systemd[1]: Starting kalshi-multicategory-refresh-scheduler.service - Kalshi paper-only multi-category refresh scheduler...",
            "Jul 13 04:37:39 kalshi-bot-01 runner[1]: [phase3bb-r35] db-writer-monitor JSON parse failed; skip writer-gated job",
            "Jul 13 04:37:39 kalshi-bot-01 runner[1]: [phase3bb-r35] Writer active; skip writer-gated job weather_fast_lane",
            "Jul 13 04:39:55 kalshi-bot-01 systemd[1]: Finished kalshi-multicategory-refresh-scheduler.service - Kalshi paper-only multi-category refresh scheduler.",
            "Jul 13 04:52:43 kalshi-bot-01 systemd[1]: Starting kalshi-multicategory-refresh-scheduler.service - Kalshi paper-only multi-category refresh scheduler...",
            "Jul 13 04:52:50 kalshi-bot-01 runner[1]: [phase3bb-r35] running weather_current_catalog_refresh",
            "Jul 13 04:52:55 kalshi-bot-01 runner[1]: Synced 10 markets.",
            "Jul 13 04:53:15 kalshi-bot-01 runner[1]: Market leg parse summary",
            "Jul 13 04:53:18 kalshi-bot-01 runner[1]: Inserted 156 weather forecast row(s) and 0 observation row(s) from noaa.",
            "Jul 13 04:53:21 kalshi-bot-01 runner[1]: Processed 156 weather forecast row(s) for new_york and inserted 156 feature row(s).",
            "Jul 13 04:53:25 kalshi-bot-01 runner[1]: Wrote JSON: reports/phase3az_r12_weather/weather_activation_preview.json",
            "Jul 13 04:53:30 kalshi-bot-01 runner[1]: [phase3bb-r35] running weather_fast_lane",
            "Jul 13 04:54:10 kalshi-bot-01 runner[1]: Wrote JSON: reports/phase3bb_r2/weather_funnel.json",
            "Jul 13 04:55:26 kalshi-bot-01 systemd[1]: Finished kalshi-multicategory-refresh-scheduler.service - Kalshi paper-only multi-category refresh scheduler.",
        ]
    )
    outputs = {
        "remote_time_utc": ("2026-07-13T05:10:00Z\n", True, 0, ""),
        "scheduler_timer_active": ("active\n", True, 0, ""),
        "scheduler_timer_enabled": ("enabled\n", True, 0, ""),
        "scheduler_service_active": ("inactive\n", True, 0, ""),
        "scheduler_timer_list": (
            "NEXT LEFT LAST PASSED UNIT ACTIVATES\n"
            "Mon 2026-07-13 05:07:50 UTC 11min Mon 2026-07-13 04:52:43 UTC 3min ago kalshi-multicategory-refresh-scheduler.timer kalshi-multicategory-refresh-scheduler.service\n",
            True,
            0,
            "",
        ),
        "scheduler_service_systemd": (
            "LoadState=loaded\nActiveState=inactive\nSubState=dead\nExecMainPID=0\nResult=success\nNRestarts=0\n",
            True,
            0,
            "",
        ),
        "scheduler_timer_systemd": (
            "LoadState=loaded\nActiveState=active\nSubState=waiting\nUnitFileState=enabled\n",
            True,
            0,
            "",
        ),
        "scheduler_journal": (journal, True, 0, ""),
        "scheduler_runner_script": (_runner_with_weather_catalog_hook(), True, 0, ""),
        "ui_service_active": ("active\n", True, 0, ""),
        "tailscale_serve_status": (
            "https://kalshi-bot-01.taile570d1.ts.net (tailnet only)\n"
            "|-- / proxy http://127.0.0.1:8080\n",
            True,
            0,
            "",
        ),
        "ui_local_http": ("200 text/html; charset=utf-8\n", True, 0, ""),
        "r5_status_json": (
            json.dumps(
                {
                    "pid": 10573,
                    "process": {
                        "phase3bc_r5_process_running": True,
                        "phase3bc_r5_pids": [10573],
                    },
                    "guard": {"status": "RUNNING", "should_stop": False},
                    "latest_watch_state": "WAITING_FOR_POSITIVE_EV",
                    "latest_summary": {"positive_ev_rows": 0, "paper_ready_candidates": 0},
                }
            ),
            True,
            0,
            "",
        ),
        "r5_processes": (
            "10573 /opt/kalshi-predictive-bot/.venv/bin/python phase3bc-r5-crypto-freshness-watch\n",
            True,
            0,
            "",
        ),
        "db_writer_monitor": (
            json.dumps({"status": "WRITER_IDLE", "safe_to_start_write": True}),
            True,
            0,
            "",
        ),
        "latest_reports": (
            "reports/phase3bb_r33/cloud_paper_only_operations_readiness.json|1783920000|100\n"
            "reports/phase3bb_r8/unified_paper_gate.md|1783920000|100\n"
            "reports/phase3bc_r5/phase3bc_r5_status.json|1783920000|100\n",
            True,
            0,
            "",
        ),
        "command_registry": ("COMMAND_REGISTRY_OK\n", True, 0, ""),
    }
    if overrides:
        outputs.update(overrides)

    def _runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        stdout, ok, exit_code, stderr = outputs.get(probe.name, ("", True, 0, ""))
        return RemoteProbeResult(
            name=probe.name,
            command=probe.command,
            ok=ok,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.01,
        )

    return _runner


def _runner_with_weather_catalog_hook() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

run_job() {
  local job_id="$1"
  shift 2
  echo "[phase3bb-r35] running ${job_id}"
  "$@"
}

# cadence_minutes=30 category=weather-catalog
run_job weather_current_catalog_refresh true bash -lc 'set -euo pipefail; .venv/bin/kalshi-bot sync-markets --status open --limit 100 --max-pages 3 --series-ticker KXTEMPNYCH; .venv/bin/kalshi-bot market-legs-parse --refresh --limit 1500; .venv/bin/kalshi-bot phase3az-r12-weather-activation-preview --output-dir reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 --match-tolerance-hours 3'

# cadence_minutes=30 category=weather
run_job weather_fast_lane true .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane --output-dir reports/phase3bb_r2 --reports-dir reports
"""
