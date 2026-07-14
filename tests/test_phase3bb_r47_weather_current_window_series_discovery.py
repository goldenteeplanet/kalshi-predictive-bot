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
from kalshi_predictor.phase3bb_r47_weather_current_window_series_discovery import (
    build_phase3bb_r47_weather_current_window_series_discovery,
    patch_runner_weather_feature_refresh,
    write_phase3bb_r47_weather_current_window_series_discovery_report,
)


def test_patch_runner_weather_feature_refresh_adds_source_steps() -> None:
    patched = patch_runner_weather_feature_refresh(
        _old_runner(),
        repaired_block=_repaired_block(),
    )

    assert "ingest-weather --location-key new_york" in patched
    assert "build-weather-features --location-key new_york" in patched
    assert patched.find("build-weather-features") < patched.find("phase3az-r12-weather-activation-preview")
    assert patched.find("weather_current_catalog_refresh") < patched.find("weather_fast_lane")
    assert patched == patch_runner_weather_feature_refresh(patched, repaired_block=_repaired_block())


def test_phase3bb_r47_ready_to_install_when_features_are_stale(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r47_weather_current_window_series_discovery_report(
            session,
            output_dir=reports_dir / "phase3bb_r47",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["linkability_decision"]
    assert decision["status"] == "READY_TO_INSTALL_WEATHER_FEATURE_REFRESH_HOOK"
    assert decision["recommended_series_ticker"] == "KXTEMPNYCH"
    assert decision["recommended_location_key"] == "new_york"
    assert decision["current_weather_market_rows"] == 10
    assert decision["missing_current_weather_link_rows"] == 10
    assert decision["fresh_feature_window_missing_rows"] == 10
    assert decision["runner_patch_required"] is True
    assert all(row["passed"] for row in payload["linkability_checks"])
    assert artifacts.series_csv_path.exists()
    assert artifacts.linkability_csv_path.exists()


def test_phase3bb_r47_apply_installs_weather_feature_hook(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r47_weather_current_window_series_discovery(
            session,
            output_dir=reports_dir / "phase3bb_r47",
            reports_dir=reports_dir,
            apply=True,
            backup_first=True,
            probe_runner=_fake_probe_runner(post_apply=True),
        )

    decision = payload["linkability_decision"]
    assert decision["status"] == "WEATHER_FEATURE_REFRESH_HOOK_INSTALLED"
    assert decision["runner_repaired_after"] is True
    assert payload["install_result"]["attempted"] is True
    assert payload["install_result"]["ok"] is True
    assert payload["safety_flags"]["scheduler_runner_written_to_system"] is True
    assert payload["safety_flags"]["scheduler_timer_started"] is False
    assert payload["safety_flags"]["creates_paper_trades"] is False


def test_phase3bb_r47_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r47-weather-current-window-series-discovery-linkability-repair", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r47-weather-current-window-series-discovery-linkability-repair" in result.output
    assert "--backup-first" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r47.db'}")
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


def _fake_probe_runner(*, post_apply: bool = False):
    old_runner = _old_runner()
    patched_runner = patch_runner_weather_feature_refresh(
        old_runner,
        repaired_block=_repaired_block(),
    )
    call_counts: dict[str, int] = {}

    def _runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        call_counts[probe.name] = call_counts.get(probe.name, 0) + 1
        if probe.name == "runner_script":
            stdout = patched_runner if post_apply and call_counts[probe.name] > 1 else old_runner
        elif probe.name == "verify_runner_after_r47_install":
            stdout = patched_runner
        elif probe.name == "scheduler_service_active":
            stdout = "inactive\n"
        elif probe.name == "scheduler_service_show":
            stdout = "ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\n"
        elif probe.name == "scheduler_timer_active":
            stdout = "active\n"
        elif probe.name == "db_writer_monitor_raw":
            stdout = json.dumps({"status": "OK", "safe_to_start_write": True})
        elif probe.name == "weather_current_window_snapshot":
            stdout = json.dumps(_snapshot_payload())
        elif probe.name == "weather_activation_preview_json":
            stdout = json.dumps({"summary": {"rows_safe_to_link": 0, "rows_safe_to_relink": 0}})
        elif probe.name == "weather_funnel_json":
            stdout = json.dumps({"summary": {"current_weather_rows": 0, "ranking_rows": 0}})
        elif probe.name == "command_registry":
            stdout = "COMMAND_REGISTRY_OK\n"
        elif probe.name == "install_weather_feature_refresh_hook":
            stdout = "INSTALLED_R47_WEATHER_FEATURE_REFRESH_HOOK\n"
        elif probe.name == "remote_time_utc":
            stdout = "2026-07-13T18:00:00Z\n"
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


def _snapshot_payload() -> dict[str, object]:
    return {
        "ok": True,
        "summary": {
            "active_weather_markets_sampled": 10,
            "current_weather_market_rows": 10,
            "current_series_count": 1,
            "missing_current_weather_link_rows": 10,
            "ready_for_r12_safe_link_preview_rows": 0,
            "fresh_feature_window_missing_rows": 10,
            "current_link_exists_rows": 0,
            "location_unknown_rows": 0,
            "blocker_counts": {"FRESH_FEATURE_WINDOW_MISSING": 10},
        },
        "current_weather_series": [
            {
                "series_ticker": "KXTEMPNYCH",
                "location_key": "new_york",
                "current_market_rows": 10,
                "missing_link_rows": 10,
                "linked_rows": 0,
                "fresh_feature_window_missing_rows": 10,
                "ready_for_r12_preview_rows": 0,
                "sample_ticker": "KXTEMPNYCH-26JUL1314-T80.99",
                "max_target_time": "2026-07-13T18:00:00+00:00",
            }
        ],
        "linkability_rows": [
            {
                "ticker": "KXTEMPNYCH-26JUL1314-T80.99",
                "series_ticker": "KXTEMPNYCH",
                "location_key": "new_york",
                "target_time": "2026-07-13T18:00:00+00:00",
                "has_weather_link": False,
                "blocker": "FRESH_FEATURE_WINDOW_MISSING",
            }
        ],
        "feature_windows": [
            {
                "location_key": "new_york",
                "feature_rows_sampled": 1560,
                "max_generated_at": "2026-07-10T19:53:10+00:00",
                "max_generated_age_hours": 69.0,
            }
        ],
    }


def _repaired_block() -> str:
    return "\n".join(
        [
            "# cadence_minutes=30 category=weather-catalog",
            (
                "run_job weather_current_catalog_refresh true bash -lc 'set -euo pipefail; "
                ".venv/bin/kalshi-bot sync-markets --status open --limit 100 --max-pages 3 --series-ticker KXTEMPNYCH; "
                ".venv/bin/kalshi-bot market-legs-parse --refresh --limit 1500; "
                ".venv/bin/kalshi-bot ingest-weather --location-key new_york; "
                ".venv/bin/kalshi-bot build-weather-features --location-key new_york; "
                ".venv/bin/kalshi-bot phase3az-r12-weather-activation-preview --output-dir reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 --match-tolerance-hours 3'"
            ),
        ]
    )


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
