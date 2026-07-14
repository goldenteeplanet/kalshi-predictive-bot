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
from kalshi_predictor.phase3bb_r43_weather_catalog_scheduler_hook import (
    HOOK_JOB_ID,
    build_phase3bb_r43_weather_catalog_scheduler_hook,
    write_phase3bb_r43_weather_catalog_scheduler_hook_report,
)


def test_phase3bb_r43_reports_ready_to_install_hook(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    _write_r42(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r43_weather_catalog_scheduler_hook_report(
            session,
            output_dir=reports_dir / "phase3bb_r43",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["hook_decision"]
    assert payload["phase"] == "3BB-R43-WEATHER-CATALOG-SCHEDULER-HOOK"
    assert decision["status"] == "READY_TO_INSTALL_WEATHER_CATALOG_HOOK"
    assert decision["runner_patch_required"] is True
    assert HOOK_JOB_ID in payload["patched_runner_script"]
    assert all(row["passed"] for row in payload["hook_checks"])
    assert artifacts.runner_draft_path.exists()


def test_phase3bb_r43_applies_hook_with_backup(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    _write_r42(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r43_weather_catalog_scheduler_hook(
            session,
            output_dir=reports_dir / "phase3bb_r43",
            reports_dir=reports_dir,
            apply=True,
            backup_first=True,
            probe_runner=_fake_probe_runner(),
        )

    decision = payload["hook_decision"]
    assert decision["status"] == "WEATHER_CATALOG_HOOK_INSTALLED"
    assert decision["install_attempted"] is True
    assert decision["runner_hook_present_after"] is True
    assert payload["install_result"]["ok"] is True


def test_phase3bb_r43_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r43-weather-catalog-scheduler-hook", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r43-weather-catalog-scheduler-hook" in result.output
    assert "--backup-first" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r43.db'}")
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


def _write_r42(reports_dir: Path) -> None:
    r42_dir = reports_dir / "phase3bb_r42"
    r42_dir.mkdir(parents=True, exist_ok=True)
    (r42_dir / "weather_fast_lane_post_unblock.json").write_text(
        json.dumps(
            {
                "post_unblock_decision": {
                    "status": "WEATHER_FAST_LANE_POST_UNBLOCK_VERIFIED",
                    "first_hard_blocker": "NO_CURRENT_WEATHER_ROWS",
                },
                "weather_fast_lane_summary": {"first_hard_blocker": "NO_CURRENT_WEATHER_ROWS"},
            }
        ),
        encoding="utf-8",
    )


def _fake_probe_runner(overrides: dict[str, tuple[str, bool, int | None, str]] | None = None):
    outputs = {
        "remote_time_utc": ("2026-07-13T14:00:00Z\n", True, 0, ""),
        "db_writer_monitor_raw": (
            json.dumps({"status": "OPEN_READERS", "safe_to_start_write": True, "current_writer_pid": None}),
            True,
            0,
            "",
        ),
        "db_writer_monitor_json_tool": ("", True, 0, ""),
        "scheduler_timer_active": ("active\n", True, 0, ""),
        "scheduler_service_active": ("inactive\n", True, 0, ""),
        "scheduler_runner_script": (_runner_without_hook(), True, 0, ""),
        "command_registry": ("COMMAND_REGISTRY_OK\n", True, 0, ""),
        "weather_funnel_json": (
            json.dumps({"status": "NO_CURRENT_WEATHER_ROWS", "summary": {"current_weather_rows": 0}}),
            True,
            0,
            "",
        ),
        "install_runner_hook": ("INSTALLED_R43_WEATHER_CATALOG_HOOK\nbackup=/tmp/old.bak\n", True, 0, ""),
        "runner_script_after_apply": (_runner_with_hook(), True, 0, ""),
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


def _runner_without_hook() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

cd /opt/kalshi-predictive-bot
run_job() {
  local job_id="$1"
  shift 2
  echo "[phase3bb-r35] running ${job_id}"
  "$@"
}

# cadence_minutes=30 category=weather
run_job weather_fast_lane true .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane --output-dir reports/phase3bb_r2 --reports-dir reports
"""


def _runner_with_hook() -> str:
    return _runner_without_hook().replace(
        "# cadence_minutes=30 category=weather\n",
        "# cadence_minutes=30 category=weather-catalog\n"
        "run_job weather_current_catalog_refresh true bash -lc 'set -euo pipefail; .venv/bin/kalshi-bot sync-markets --status open --limit 100 --max-pages 3 --series-ticker KXTEMPNYCH'\n\n"
        "# cadence_minutes=30 category=weather\n",
    )
