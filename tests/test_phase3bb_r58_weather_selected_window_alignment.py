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
from kalshi_predictor.phase3bb_r58_weather_selected_window_alignment import (
    write_phase3bb_r58_weather_selected_window_alignment_report,
)


def test_phase3bb_r58_reports_r57_patch_and_alignment_gap(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    _write_r57_payload(reports_dir)
    calls: list[RemoteProbe] = []

    with session_factory() as session:
        artifacts = write_phase3bb_r58_weather_selected_window_alignment_report(
            session,
            output_dir=reports_dir / "phase3bb_r58",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(calls=calls),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["decision"]["status"] == "R57_PATCHED_RERUN_SELECTED_WINDOW_PIPELINE"
    assert payload["decision"]["first_hard_blocker"] == "FORECAST_MISSING_AFTER_FEATURE_ALIGNMENT"
    assert payload["summary"]["feature_aligned_rows"] == 2
    assert payload["summary"]["forecast_rows"] == 0
    assert payload["summary"]["r57_patch_complete"] is True
    assert "phase3bb-r57-weather-selected-window-pipeline-speed-repair" in payload["next_operator_command"]
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert any(probe.name == "selected_window_alignment" for probe in calls)
    assert artifacts.rows_csv_path.exists()
    assert artifacts.patch_status_csv_path.exists()


def test_phase3bb_r58_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair" in result.output
    assert "match-tolerance-hours" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r58.db'}")
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


def _write_r57_payload(reports_dir: Path) -> None:
    r57_dir = reports_dir / "phase3bb_r57"
    r57_dir.mkdir(parents=True, exist_ok=True)
    (r57_dir / "selected_window_weather_pipeline.json").write_text(
        json.dumps(
            {
                "r53_final_payload": {
                    "summary": {
                        "selected_target_time": "2026-07-14T05:00:00+00:00",
                        "selected_minutes_until_target": 30,
                        "selected_window_market_rows": 2,
                        "selected_window_forecast_rows": 0,
                        "selected_window_ranking_rows": 0,
                    }
                },
                "selected_window_tickers": [
                    {"ticker": "KXTEMPNYCH-26JUL1405-T76.99"},
                    {"ticker": "KXTEMPNYCH-26JUL1405-T77.99"},
                ],
                "remote_probe_results": [
                    {
                        "name": "weather_per_ticker_forecast",
                        "stdout_excerpt": "PHASE3BB_R57_FORECASTED_TICKERS=0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _fake_probe_runner(*, calls: list[RemoteProbe]):
    def _runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        calls.append(probe)
        if probe.name == "remote_time_utc":
            stdout = "2026-07-14T04:00:00Z\n"
        elif probe.name == "command_registry":
            stdout = "COMMAND_REGISTRY_OK\n"
        elif probe.name == "selected_window_alignment":
            stdout = json.dumps(
                {
                    "ok": True,
                    "selected_target_time": "2026-07-14T05:00:00+00:00",
                    "selected_ticker_count": 2,
                    "rows": [
                        {
                            "ticker": "KXTEMPNYCH-26JUL1405-T76.99",
                            "market_target_time": "2026-07-14T05:00:00+00:00",
                            "link_target_time": "2026-07-14T05:00:00+00:00",
                            "feature_target_time": "2026-07-14T05:00:00+00:00",
                            "feature_distance_hours": 0,
                            "first_alignment_blocker": "FORECAST_MISSING",
                        },
                        {
                            "ticker": "KXTEMPNYCH-26JUL1405-T77.99",
                            "market_target_time": "2026-07-14T05:00:00+00:00",
                            "link_target_time": "2026-07-14T05:00:00+00:00",
                            "feature_target_time": "2026-07-14T05:00:00+00:00",
                            "feature_distance_hours": 0,
                            "first_alignment_blocker": "FORECAST_MISSING",
                        },
                    ],
                    "summary": {
                        "row_count": 2,
                        "feature_aligned_rows": 2,
                        "forecast_rows": 0,
                        "ranking_rows": 0,
                        "positive_ev_aligned_rows": 0,
                        "blocker_counts": {"FORECAST_MISSING": 2},
                    },
                }
            )
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
