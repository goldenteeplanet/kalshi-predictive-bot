from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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
from kalshi_predictor.phase3bb_r48_weather_feature_refresh_runtime_verification import (
    build_phase3bb_r48_weather_feature_refresh_runtime_verification,
    write_phase3bb_r48_weather_feature_refresh_runtime_verification_report,
)
from kalshi_predictor.phase3bb_r47_weather_current_window_series_discovery import (
    _weather_current_window_snapshot_command,
)


def test_r48_exact_new_york_rows_survive_large_fresh_feature_catalog(tmp_path: Path) -> None:
    db_path = tmp_path / "r48_exact_match.db"
    now = datetime.now(timezone.utc)
    target = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    generated = now - timedelta(minutes=5)
    tickers = [f"KXTEMPNYCH-26JUL1514-T{value}.99" for value in range(88, 98)]

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            create table markets (
                ticker text, series_ticker text, title text, subtitle text, status text,
                close_time text, expected_expiration_time text, expiration_time text,
                settlement_ts text
            );
            create table weather_market_links (ticker text);
            create table weather_features (
                id integer primary key, location_key text, source text, generated_at text,
                target_time text, temperature_f real, weather_confidence_score real
            );
            """
        )
        for ticker in tickers:
            conn.execute(
                "insert into markets values (?, 'KXTEMPNYCH', ?, '', 'active', ?, null, null, null)",
                (ticker, "New York City temperature at 2pm EDT", target.replace(tzinfo=None).isoformat(sep=" ")),
            )
        # These rows reproduce the old global-LIMIT failure: all are fresher and sort
        # ahead of the exact target, but none belongs to the candidate-time window.
        distractor_target = target + timedelta(hours=24)
        conn.executemany(
            "insert into weather_features values (?, 'new_york', 'stored_forecasts', ?, ?, 80, 1)",
            [
                (
                    row_id,
                    (generated + timedelta(seconds=1)).replace(tzinfo=None).isoformat(sep=" "),
                    distractor_target.replace(tzinfo=None).isoformat(sep=" "),
                )
                for row_id in range(1, 6002)
            ],
        )
        conn.execute(
            "insert into weather_features values (7000, 'new_york', 'stored_forecasts', ?, ?, 82, 1)",
            (
                generated.replace(tzinfo=None).isoformat(sep=" "),
                target.replace(tzinfo=None).isoformat(sep=" "),
            ),
        )

    command = _weather_current_window_snapshot_command(
        str(db_path),
        current_window_lookback_hours=3,
        fresh_window_hours=24,
        match_tolerance_hours=3,
    )
    script = command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    result = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    rows = [row for row in payload["linkability_rows"] if row["ticker"] in tickers]

    assert len(rows) == 10
    assert {row["matched_fresh_feature_id"] for row in rows} == {7000}
    assert {row["matched_fresh_feature_source"] for row in rows} == {"stored_forecasts"}
    assert {row["matched_fresh_feature_distance_hours"] for row in rows} == {0.0}
    assert {row["blocker"] for row in rows} == {"READY_FOR_R12_SAFE_LINK_PREVIEW"}


def test_phase3bb_r48_verifies_feature_refresh_and_opens_link_gate(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r48_weather_feature_refresh_runtime_verification_report(
            session,
            output_dir=reports_dir / "phase3bb_r48",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["runtime_decision"]
    parsed = payload["parsed_runtime_state"]
    assert payload["phase"] == "3BB-R48-WEATHER-FEATURE-REFRESH-RUNTIME-VERIFICATION"
    assert decision["status"] == "WEATHER_FEATURE_REFRESH_RUNTIME_VERIFIED_LINK_GATE_READY"
    assert decision["feature_refresh_runtime_observed"] is True
    assert decision["fresh_feature_window_missing_rows"] == 0
    assert decision["rows_safe_to_link"] == 10
    assert parsed["feature_refresh_sequence"]["status"] == "FEATURE_REFRESH_THEN_PREVIEW_VERIFIED"
    assert "phase3bb-r49-weather-missing-link-apply-after-feature-refresh" in decision["operator_next_command"]
    assert payload["safety_flags"]["remote_db_writes_performed"] == 0
    assert payload["safety_flags"]["runs_weather_forecast"] is False
    assert all(row["passed"] for row in payload["runtime_checks"])
    assert artifacts.feature_events_csv_path.exists()
    assert artifacts.feature_windows_csv_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3bb_r48_waits_when_repaired_cycle_has_not_run(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r48_weather_feature_refresh_runtime_verification(
            session,
            output_dir=reports_dir / "phase3bb_r48",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(
                scheduler_journal=_journal_without_feature_refresh(),
                weather_snapshot=json.dumps(_stale_snapshot()),
                weather_preview=json.dumps({"summary": {"rows_safe_to_link": 0, "rows_safe_to_relink": 0}}),
            ),
        )

    decision = payload["runtime_decision"]
    assert decision["status"] == "WAIT_FOR_NEXT_SCHEDULER_CYCLE"
    assert decision["first_weather_blocker"] == "FEATURE_REFRESH_RUNTIME_NOT_OBSERVED"
    assert decision["fresh_feature_window_missing_rows"] == 10
    assert "phase3bb-r48-weather-feature-refresh-runtime-verification" in decision["operator_next_command"]
    assert payload["safety_flags"]["systemctl_start_stop_restart_executed"] == 0


def test_phase3bb_r48_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r48-weather-feature-refresh-runtime-verification", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r48-weather-feature-refresh-runtime-verification" in result.output
    assert "--journal-lines" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r48.db'}")
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


def _fake_probe_runner(
    *,
    scheduler_journal: str | None = None,
    weather_snapshot: str | None = None,
    weather_preview: str | None = None,
):
    scheduler_journal = scheduler_journal or _journal_with_feature_refresh()
    weather_snapshot = weather_snapshot or json.dumps(_fresh_snapshot())
    weather_preview = weather_preview or json.dumps(
        {
            "summary": {
                "active_weather_markets_reviewed": 30,
                "rows_safe_to_link": 10,
                "rows_safe_to_relink": 0,
                "first_blocker": "SAFE_TO_LINK",
            }
        }
    )
    outputs = {
        "remote_time_utc": ("2026-07-13T19:35:00Z\n", True, 0, ""),
        "scheduler_timer_active": ("active\n", True, 0, ""),
        "scheduler_service_active": ("inactive\n", True, 0, ""),
        "scheduler_service_show": ("ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\n", True, 0, ""),
        "scheduler_timer_list": (
            "NEXT LEFT LAST PASSED UNIT ACTIVATES\n"
            "Mon 2026-07-13 19:45:00 UTC 10min Mon 2026-07-13 19:15:00 UTC 20min ago kalshi-multicategory-refresh-scheduler.timer kalshi-multicategory-refresh-scheduler.service\n",
            True,
            0,
            "",
        ),
        "scheduler_journal": (scheduler_journal, True, 0, ""),
        "scheduler_runner_script": (_runner_with_feature_refresh(), True, 0, ""),
        "db_writer_monitor_raw": (json.dumps({"status": "OPEN_READERS", "safe_to_start_write": True}), True, 0, ""),
        "r47_json": (
            json.dumps({"linkability_decision": {"status": "WEATHER_FEATURE_REFRESH_HOOK_INSTALLED", "runner_repaired_after": True}}),
            True,
            0,
            "",
        ),
        "weather_activation_preview_json": (weather_preview, True, 0, ""),
        "weather_funnel_json": (
            json.dumps({"status": "NO_CURRENT_WEATHER_ROWS", "summary": {"current_weather_rows": 0, "ranking_rows": 0}}),
            True,
            0,
            "",
        ),
        "r40_json": (
            json.dumps(
                {
                    "parsed_runtime_state": {
                        "weather_source_ingest_event_count": 1,
                        "weather_feature_build_event_count": 1,
                    }
                }
            ),
            True,
            0,
            "",
        ),
        "weather_current_window_snapshot": (weather_snapshot, True, 0, ""),
        "weather_report_stats": (_report_stats(), True, 0, ""),
        "command_registry": ("COMMAND_REGISTRY_OK\n", True, 0, ""),
    }

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


def _runner_with_feature_refresh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

# cadence_minutes=30 category=weather-catalog
run_job weather_current_catalog_refresh true bash -lc 'set -euo pipefail; .venv/bin/kalshi-bot sync-markets --status open --limit 100 --max-pages 3 --series-ticker KXTEMPNYCH; .venv/bin/kalshi-bot market-legs-parse --refresh --limit 1500; .venv/bin/kalshi-bot ingest-weather --location-key new_york; .venv/bin/kalshi-bot build-weather-features --location-key new_york; .venv/bin/kalshi-bot phase3az-r12-weather-activation-preview --output-dir reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 --match-tolerance-hours 3'

# cadence_minutes=30 category=weather
run_job weather_fast_lane true .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane --output-dir reports/phase3bb_r2 --reports-dir reports
"""


def _journal_with_feature_refresh() -> str:
    return "\n".join(
        [
            "Jul 13 19:15:00 kalshi-bot-01 runner[1]: [phase3bb-r35] running weather_current_catalog_refresh",
            "Jul 13 19:15:02 kalshi-bot-01 runner[1]: Synced 30 markets.",
            "Jul 13 19:15:12 kalshi-bot-01 runner[1]: Market leg parse summary",
            "Jul 13 19:15:18 kalshi-bot-01 runner[1]: Inserted 156 weather forecast row(s) and 0 observation row(s) from noaa.",
            "Jul 13 19:15:21 kalshi-bot-01 runner[1]: Processed 156 weather forecast row(s) for new_york and inserted 156 feature row(s).",
            "Jul 13 19:15:25 kalshi-bot-01 runner[1]: Wrote JSON: reports/phase3az_r12_weather/weather_activation_preview.json",
            "Jul 13 19:15:28 kalshi-bot-01 runner[1]: [phase3bb-r35] running weather_fast_lane",
        ]
    )


def _journal_without_feature_refresh() -> str:
    return "\n".join(
        [
            "Jul 13 19:15:00 kalshi-bot-01 runner[1]: [phase3bb-r35] running weather_current_catalog_refresh",
            "Jul 13 19:15:02 kalshi-bot-01 runner[1]: Synced 30 markets.",
            "Jul 13 19:15:12 kalshi-bot-01 runner[1]: Market leg parse summary",
            "Jul 13 19:15:25 kalshi-bot-01 runner[1]: Wrote JSON: reports/phase3az_r12_weather/weather_activation_preview.json",
        ]
    )


def _fresh_snapshot() -> dict[str, object]:
    return {
        "ok": True,
        "summary": {
            "current_weather_market_rows": 10,
            "missing_current_weather_link_rows": 10,
            "fresh_feature_window_missing_rows": 0,
            "ready_for_r12_safe_link_preview_rows": 10,
        },
        "current_weather_series": [
            {
                "series_ticker": "KXTEMPNYCH",
                "location_key": "new_york",
                "current_market_rows": 10,
                "missing_link_rows": 10,
                "fresh_feature_window_missing_rows": 0,
                "ready_for_r12_preview_rows": 10,
            }
        ],
        "feature_windows": [
            {
                "location_key": "new_york",
                "feature_rows_sampled": 156,
                "max_generated_at": "2026-07-13T19:15:21+00:00",
                "max_generated_age_hours": 0.3,
            }
        ],
        "linkability_rows": [
            {
                "ticker": "KXTEMPNYCH-26JUL1315-T80.99",
                "location_key": "new_york",
                "target_time": "2026-07-13T19:00:00+00:00",
                "has_weather_link": False,
                "blocker": "READY_FOR_R12_SAFE_LINK_PREVIEW",
            }
        ],
    }


def _stale_snapshot() -> dict[str, object]:
    payload = _fresh_snapshot()
    payload["summary"] = {
        "current_weather_market_rows": 10,
        "missing_current_weather_link_rows": 10,
        "fresh_feature_window_missing_rows": 10,
        "ready_for_r12_safe_link_preview_rows": 0,
    }
    payload["feature_windows"] = [
        {
            "location_key": "new_york",
            "feature_rows_sampled": 1560,
            "max_generated_at": "2026-07-10T19:53:10+00:00",
            "max_generated_age_hours": 71.0,
        }
    ]
    return payload


def _report_stats() -> str:
    return "\n".join(
        f"{path}|1783969821|100"
        for path in [
            "reports/phase3bb_r47/weather_current_window_series_discovery.json",
            "reports/phase3az_r12_weather/weather_activation_preview.json",
            "reports/phase3az_r12_weather/safe_to_link.csv",
            "reports/phase3az_r12_weather/safe_to_relink.csv",
            "reports/phase3bb_r2/weather_funnel.json",
            "reports/phase3bb_r40/cloud_scheduler_runtime_monitor.json",
        ]
    )
