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
from kalshi_predictor.phase3bb_r57_weather_selected_window_pipeline import (
    build_phase3bb_r57_weather_selected_window_pipeline,
    write_phase3bb_r57_weather_selected_window_pipeline_report,
)


def test_phase3bb_r57_runs_selected_window_per_ticker_pipeline(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    calls: list[RemoteProbe] = []

    with session_factory() as session:
        artifacts = write_phase3bb_r57_weather_selected_window_pipeline_report(
            session,
            output_dir=reports_dir / "phase3bb_r57",
            reports_dir=reports_dir,
            r53_output_dir=reports_dir / "phase3bb_r53",
            expected_writer_pid=88544,
            max_wait_seconds=1,
            poll_interval_seconds=0,
            probe_runner=_fake_probe_runner(
                calls=calls,
                writer_wait=[
                    _writer_state(safe=False, pid=88544),
                    _writer_state(safe=True, pid=None),
                ],
                r53_states=[
                    _r53_state(missing_links=10, feature_rows=0, forecast_rows=0, ranking_rows=0),
                    _r53_state(missing_links=0, feature_rows=0, forecast_rows=0, ranking_rows=0),
                    _r53_state(missing_links=0, feature_rows=10, forecast_rows=10, ranking_rows=10, positive_rows=2),
                ],
                paper_gate_summary={
                    "weather_rows": 10,
                    "weather_positive_ev_rows": 2,
                    "weather_paper_ready_rows": 0,
                    "first_weather_positive_ticker": "KXTEMPNYCH-26JUL1405-T75.99",
                    "first_weather_positive_blocker": "PHASE_3M_ZERO_SIZE",
                },
            ),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["decision"]
    call_names = [probe.name for probe in calls]
    forecast_probe = next(probe for probe in calls if probe.name == "weather_per_ticker_forecast")

    assert decision["status"] == "WEATHER_POSITIVE_EV_NOT_PAPER_READY"
    assert decision["first_hard_blocker"] == "PHASE_3M_ZERO_SIZE"
    assert payload["r12_apply_summary"]["link_rows_written"] == 10
    assert payload["pipeline_gate"]["allowed"] is True
    assert call_names.index("r12_missing_link_apply") < call_names.index("weather_feature_refresh")
    assert call_names.index("weather_snapshot_capture") < call_names.index("weather_per_ticker_forecast")
    assert "forecast --model weather_v2 --ticker" in forecast_probe.command
    assert "selected_tickers = [" in forecast_probe.command
    assert "KXTEMPNYCH-26JUL1405-T69.99" in forecast_probe.command
    assert "PHASE3BB_R57_SELECTED_TICKERS=" in forecast_probe.command
    assert "--limit 500" not in forecast_probe.command
    assert "--limit 1" in forecast_probe.command
    assert payload["safety_flags"]["uses_broad_weather_forecast"] is False
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert artifacts.selected_tickers_csv_path.exists()


def test_phase3bb_r57_does_not_run_pipeline_when_writer_never_clears(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    calls: list[RemoteProbe] = []

    with session_factory() as session:
        payload = build_phase3bb_r57_weather_selected_window_pipeline(
            session,
            output_dir=reports_dir / "phase3bb_r57",
            reports_dir=reports_dir,
            r53_output_dir=reports_dir / "phase3bb_r53",
            expected_writer_pid=88544,
            max_wait_seconds=1,
            poll_interval_seconds=0,
            probe_runner=_fake_probe_runner(
                calls=calls,
                writer_wait=[
                    _writer_state(safe=False, pid=88544),
                    _writer_state(safe=False, pid=88544),
                ],
                r53_states=[
                    _r53_state(missing_links=0, feature_rows=10, forecast_rows=0, ranking_rows=0),
                ],
                paper_gate_summary={},
            ),
        )

    assert payload["decision"]["status"] == "WAITING_FOR_WRITER_CLEAR"
    assert payload["decision"]["first_hard_blocker"] == "ACTIVE_WRITER"
    assert "weather_snapshot_capture" not in [probe.name for probe in calls]
    assert payload["pipeline_steps"] == []
    assert payload["safety_flags"]["runs_weather_per_ticker_forecast"] is False


def test_phase3bb_r57_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r57-weather-selected-window-pipeline-speed-repair", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r57-weather-selected-window-pipeline-speed-repair" in result.output
    assert "per-ticker-timeout-seconds" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r57.db'}")
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
    calls: list[RemoteProbe],
    writer_wait: list[dict[str, object]],
    r53_states: list[dict[str, object]],
    paper_gate_summary: dict[str, object],
):
    state = {"writer": 0, "r53": 0}

    def _runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        calls.append(probe)
        stdout = ""
        if probe.name.startswith("writer_gate_check"):
            index = min(state["writer"], len(writer_wait) - 1)
            state["writer"] += 1
            stdout = json.dumps(writer_wait[index])
        elif probe.name == "remote_time_utc":
            stdout = "2026-07-14T04:00:00Z\n"
        elif probe.name in {"db_writer_monitor", "db_writer_monitor_post"}:
            stdout = json.dumps(_writer_state(safe=True, pid=None))
        elif probe.name == "command_registry":
            stdout = "COMMAND_REGISTRY_OK\n"
        elif probe.name == "r12_preview_json":
            stdout = json.dumps({"summary": {"rows_safe_to_link": 0, "rows_safe_to_relink": 0}})
        elif probe.name == "weather_current_window_state":
            index = min(state["r53"], len(r53_states) - 1)
            state["r53"] += 1
            stdout = json.dumps(r53_states[index])
        elif probe.name == "r12_missing_link_apply":
            stdout = json.dumps({"status": "APPLIED", "summary": {"link_rows_written": 10}})
        elif probe.name == "weather_feature_refresh":
            stdout = "Inserted 156 weather forecast row(s).\nProcessed 4524 weather forecast row(s).\n"
        elif probe.name == "weather_snapshot_capture":
            stdout = "Captured 10 snapshots.\n"
        elif probe.name == "weather_per_ticker_forecast":
            stdout = "PHASE3BB_R57_SELECTED_TICKERS=10\nPHASE3BB_R57_FORECASTED_TICKERS=10\n"
        elif probe.name == "weather_fast_lane_run":
            stdout = "Wrote JSON: reports/phase3bb_r2/weather_funnel.json\n"
        elif probe.name == "unified_paper_gate_run":
            stdout = "Wrote paper gate rows: reports/phase3bb_r8/paper_gate_rows.csv\n"
        elif probe.name == "ba_r5_truth_run":
            stdout = "Wrote JSON: reports/phase3ba_r5/paper_ready_truth.json\n"
        elif probe.name == "paper_gate_summary":
            stdout = json.dumps(paper_gate_summary)
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


def _writer_state(*, safe: bool, pid: int | None) -> dict[str, object]:
    return {
        "status": "OPEN_READERS" if safe else "WRITER_ACTIVE",
        "safe_to_start_write": safe,
        "current_writer_pid": pid,
        "current_writer_elapsed_seconds": 60 if pid else None,
    }


def _r53_state(
    *,
    missing_links: int,
    feature_rows: int,
    forecast_rows: int,
    ranking_rows: int,
    positive_rows: int = 0,
) -> dict[str, object]:
    rows = []
    for index in range(10):
        linked = index >= missing_links
        has_feature = index < feature_rows
        has_forecast = index < forecast_rows
        has_ranking = index < ranking_rows
        blocker = "MISSING_WEATHER_LINK"
        if linked and not has_feature:
            blocker = "FEATURE_MISSING_FOR_TARGET_WINDOW"
        elif linked and has_feature and not has_forecast:
            blocker = "FORECAST_MISSING"
        elif linked and has_forecast and not has_ranking:
            blocker = "RANKING_MISSING"
        elif linked and has_ranking and index < positive_rows:
            blocker = "POSITIVE_EV_READY_FOR_PAPER_GATE"
        elif linked and has_ranking:
            blocker = "EV_NOT_POSITIVE"
        rows.append(
            {
                "ticker": f"KXTEMPNYCH-26JUL1405-T{69 + index}.99",
                "target_time": "2026-07-14T05:00:00+00:00",
                "market_title": "Will the temp in New York City be above threshold?",
                "status": "active",
                "minutes_until_target": 30,
                "window_role": "SELECTED_NEXT_LIVE_WINDOW",
                "has_link": linked,
                "link_target_matches_window": linked,
                "has_snapshot": True,
                "snapshot_fresh": True,
                "has_source_forecast": True,
                "source_forecast_fresh": True,
                "has_weather_feature": has_feature,
                "weather_feature_fresh": has_feature,
                "has_current_forecast": has_forecast,
                "has_current_ranking": has_ranking,
                "first_window_blocker": blocker,
            }
        )
    return {
        "ok": True,
        "generated_at": "2026-07-14T04:00:00+00:00",
        "rows": rows,
        "audit": {
            "active_series_market_rows": 500,
            "future_series_market_rows": 10,
            "expired_series_market_rows": 490,
        },
        "summary": {
            "selected_target_time": "2026-07-14T05:00:00+00:00",
            "selected_minutes_until_target": 30,
            "selected_window_market_rows": 10,
            "selected_window_linked_rows": 10 - missing_links,
            "selected_window_missing_link_rows": missing_links,
            "selected_window_stale_link_rows": 0,
            "selected_window_snapshot_rows": 10,
            "selected_window_fresh_snapshot_rows": 10,
            "selected_window_source_forecast_rows": 10,
            "selected_window_feature_rows": feature_rows,
            "selected_window_forecast_rows": forecast_rows,
            "selected_window_ranking_rows": ranking_rows,
            "selected_window_positive_ev_rows": positive_rows,
            "selected_window_non_positive_ev_rows": max(0, ranking_rows - positive_rows),
            "selected_window_too_close_to_expiry": False,
            "first_window_blocker_counts": {},
            "writer_safe_to_start_write": True,
        },
    }
