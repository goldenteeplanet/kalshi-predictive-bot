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
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    build_phase3bb_r44_weather_catalog_hook_runtime_verification,
    write_phase3bb_r44_weather_catalog_hook_runtime_verification_report,
)


def test_phase3bb_r44_verifies_catalog_hook_and_r40_awareness(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r44_weather_catalog_hook_runtime_verification_report(
            session,
            output_dir=reports_dir / "phase3bb_r44",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["hook_runtime_decision"]
    parsed = payload["parsed_hook_runtime_state"]
    assert payload["phase"] == "3BB-R44-WEATHER-CATALOG-HOOK-RUNTIME-VERIFICATION"
    assert decision["status"] == "WEATHER_CATALOG_HOOK_RUNTIME_VERIFIED"
    assert decision["verification_passed"] is True
    assert decision["weather_catalog_hook_run_count"] == 1
    assert decision["weather_fast_lane_run_count"] == 1
    assert decision["r40_weather_catalog_runtime_order_ok"] is True
    assert parsed["weather_catalog_sequence"]["status"] == "CATALOG_THEN_FAST_LANE_VERIFIED"
    assert all(row["passed"] for row in payload["hook_runtime_checks"])
    assert artifacts.job_events_csv_path.exists()
    assert artifacts.report_freshness_csv_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3bb_r44_asks_for_r40_refresh_when_r40_is_stale(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    runner = _fake_probe_runner({"r40_json": (json.dumps({"parsed_runtime_state": {}}), True, 0, "")})

    with session_factory() as session:
        payload = build_phase3bb_r44_weather_catalog_hook_runtime_verification(
            session,
            output_dir=reports_dir / "phase3bb_r44",
            reports_dir=reports_dir,
            probe_runner=runner,
        )

    decision = payload["hook_runtime_decision"]
    assert decision["status"] == "WEATHER_CATALOG_RUNTIME_VERIFIED_R40_REFRESH_NEEDED"
    assert decision["first_failed_check"] == "r40_understands_scheduler_job_runs"
    assert "phase3bb-r40-cloud-scheduler-runtime-monitor" in decision["operator_next_command"]


def test_phase3bb_r44_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r44-weather-catalog-hook-runtime-verification", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r44-weather-catalog-hook-runtime-verification" in result.output
    assert "--journal-lines" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r44.db'}")
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


def _fake_probe_runner(overrides: dict[str, tuple[str, bool, int | None, str]] | None = None):
    weather_preview = json.dumps({"summary": {"rows_safe_to_link": 0, "rows_safe_to_relink": 0}})
    weather_funnel = json.dumps(
        {
            "status": "NO_CURRENT_WEATHER_ROWS",
            "summary": {"current_weather_rows": 0, "ranking_rows": 0, "paper_ready_rows": 0},
        }
    )
    r40_json = json.dumps(
        {
            "scheduler_job_runs": [
                {"event": "JOB_STARTED", "job_id": "weather_current_catalog_refresh"},
                {"event": "JOB_STARTED", "job_id": "weather_fast_lane"},
            ],
            "parsed_runtime_state": {
                "weather_catalog_hook_job_run_count": 1,
                "weather_fast_lane_job_run_count": 1,
                "weather_catalog_runtime_order_ok": True,
            },
        }
    )
    outputs = {
        "remote_time_utc": ("2026-07-13T14:30:00Z\n", True, 0, ""),
        "scheduler_timer_active": ("active\n", True, 0, ""),
        "scheduler_service_active": ("inactive\n", True, 0, ""),
        "scheduler_timer_list": (
            "NEXT LEFT LAST PASSED UNIT ACTIVATES\n"
            "Mon 2026-07-13 14:34:43 UTC 9min Mon 2026-07-13 14:19:43 UTC 5min ago kalshi-multicategory-refresh-scheduler.timer kalshi-multicategory-refresh-scheduler.service\n",
            True,
            0,
            "",
        ),
        "scheduler_runner_script": (_runner_with_hook(), True, 0, ""),
        "scheduler_journal": (_journal_with_runtime_sequence(), True, 0, ""),
        "db_writer_monitor_raw": (
            json.dumps({"status": "OPEN_READERS", "safe_to_start_write": True}),
            True,
            0,
            "",
        ),
        "weather_catalog_report_stats": (_report_stats(), True, 0, ""),
        "weather_activation_preview_json": (weather_preview, True, 0, ""),
        "weather_funnel_json": (weather_funnel, True, 0, ""),
        "r40_json": (r40_json, True, 0, ""),
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


def _runner_with_hook() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

# cadence_minutes=30 category=weather-catalog
run_job weather_current_catalog_refresh true bash -lc 'set -euo pipefail; .venv/bin/kalshi-bot sync-markets --status open --limit 100 --max-pages 3 --series-ticker KXTEMPNYCH; .venv/bin/kalshi-bot market-legs-parse --refresh --limit 1500; .venv/bin/kalshi-bot phase3az-r12-weather-activation-preview --output-dir reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 --match-tolerance-hours 3'

# cadence_minutes=30 category=weather
run_job weather_fast_lane true .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane --output-dir reports/phase3bb_r2 --reports-dir reports
"""


def _journal_with_runtime_sequence() -> str:
    return "\n".join(
        [
            "Jul 13 14:19:43 kalshi-bot-01 systemd[1]: Starting kalshi-multicategory-refresh-scheduler.service - Kalshi paper-only multi-category refresh scheduler...",
            "Jul 13 14:20:09 kalshi-bot-01 runner[1]: [phase3bb-r35] running weather_current_catalog_refresh",
            "Jul 13 14:20:14 kalshi-bot-01 runner[1]: Synced 10 markets.",
            "Jul 13 14:20:59 kalshi-bot-01 runner[1]: Market leg parse summary",
            "Jul 13 14:22:26 kalshi-bot-01 runner[1]: Wrote JSON: reports/phase3az_r12_weather/weather_activation_preview.json",
            "Jul 13 14:22:31 kalshi-bot-01 runner[1]: [phase3bb-r35] running weather_fast_lane",
            "Jul 13 14:23:45 kalshi-bot-01 runner[1]: Wrote JSON: reports/phase3bb_r2/weather_funnel.json",
        ]
    )


def _report_stats() -> str:
    return "\n".join(
        f"{path}|1783948981|100"
        for path in [
            "reports/phase3az_r12_weather/weather_activation_preview.json",
            "reports/phase3az_r12_weather/weather_activation_preview.md",
            "reports/phase3az_r12_weather/weather_activation_candidates.csv",
            "reports/phase3az_r12_weather/safe_to_link.csv",
            "reports/phase3az_r12_weather/safe_to_relink.csv",
            "reports/phase3bb_r2/weather_funnel.json",
            "reports/phase3bb_r2/weather_fast_lane.md",
            "reports/phase3bb_r2/weather_candidates.csv",
        ]
    )
