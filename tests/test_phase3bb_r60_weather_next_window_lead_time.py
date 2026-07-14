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
from kalshi_predictor.phase3bb_r60_weather_next_window_lead_time import (
    build_phase3bb_r60_weather_next_window_lead_time,
    write_phase3bb_r60_weather_next_window_lead_time_report,
)


def test_phase3bb_r60_runs_r59_when_window_inside_lead_time_band(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    calls: list[RemoteProbe] = []

    with session_factory() as session:
        artifacts = write_phase3bb_r60_weather_next_window_lead_time_report(
            session,
            output_dir=reports_dir / "phase3bb_r60",
            reports_dir=reports_dir,
            r53_output_dir=reports_dir / "phase3bb_r53",
            r57_output_dir=reports_dir / "phase3bb_r57",
            r59_output_dir=reports_dir / "phase3bb_r59",
            expected_writer_pid=137766,
            max_wait_seconds=1,
            poll_interval_seconds=0,
            min_minutes_before_target=20,
            max_minutes_before_target=90,
            probe_runner=_fake_probe_runner(
                calls=calls,
                writer_wait=[
                    _writer_state(safe=True, pid=None),
                    _writer_state(safe=True, pid=None),
                ],
                r53_states=[
                    _r53_state(minutes=50, forecast_rows=0, ranking_rows=0),
                    _r53_state(minutes=50, forecast_rows=0, ranking_rows=0),
                    _r53_state(minutes=50, forecast_rows=0, ranking_rows=0),
                    _r53_state(minutes=45, forecast_rows=10, ranking_rows=10, positive_rows=2),
                ],
                paper_gate_summary={
                    "weather_rows": 10,
                    "weather_positive_ev_rows": 2,
                    "weather_paper_ready_rows": 0,
                    "first_weather_positive_blocker": "PHASE_3M_ZERO_SIZE",
                },
            ),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    names = [probe.name for probe in calls]

    assert payload["lead_time_gate"]["allowed"] is True
    assert payload["lead_time_gate"]["reason"] == "TARGET_WINDOW_INSIDE_LEAD_TIME_BAND"
    assert payload["decision"]["status"] == "R59_R57_WEATHER_POSITIVE_EV_NOT_PAPER_READY"
    assert payload["decision"]["first_hard_blocker"] == "PHASE_3M_ZERO_SIZE"
    assert "weather_catalog_refresh_parse" in names
    assert "weather_per_ticker_forecast" in names
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert "phase3bb-r60-weather-next-window-lead-time-scheduler-repair" in artifacts.scheduler_hook_path.read_text(encoding="utf-8")


def test_phase3bb_r60_skips_when_selected_window_too_close(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    calls: list[RemoteProbe] = []

    with session_factory() as session:
        payload = build_phase3bb_r60_weather_next_window_lead_time(
            session,
            output_dir=reports_dir / "phase3bb_r60",
            reports_dir=reports_dir,
            r53_output_dir=reports_dir / "phase3bb_r53",
            r57_output_dir=reports_dir / "phase3bb_r57",
            r59_output_dir=reports_dir / "phase3bb_r59",
            min_minutes_before_target=20,
            max_minutes_before_target=90,
            probe_runner=_fake_probe_runner(
                calls=calls,
                writer_wait=[_writer_state(safe=True, pid=None)],
                r53_states=[_r53_state(minutes=5, forecast_rows=0, ranking_rows=0)],
                paper_gate_summary={},
            ),
        )

    assert payload["decision"]["status"] == "LEAD_TIME_GATE_CLOSED"
    assert payload["decision"]["first_hard_blocker"] == "TARGET_WINDOW_TOO_CLOSE_TO_EXPIRY"
    assert payload["decision"]["r59_ran"] is False
    assert "weather_catalog_refresh_parse" not in [probe.name for probe in calls]
    assert payload["safety_flags"]["runs_catalog_refresh"] is False


def test_phase3bb_r60_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r60-weather-next-window-lead-time-scheduler-repair", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r60-weather-next-window-lead-time-scheduler-repair" in result.output
    assert "max-minutes-before-target" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r60.db'}")
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
        elif probe.name == "weather_catalog_refresh_parse":
            stdout = "Synced 10 markets.\nMarket leg parse summary\n"
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
        elif probe.name == "weather_feature_refresh":
            stdout = "Inserted 156 weather forecast row(s).\n"
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
    minutes: float,
    forecast_rows: int,
    ranking_rows: int,
    positive_rows: int = 0,
) -> dict[str, object]:
    rows = []
    for index in range(10):
        has_forecast = index < forecast_rows
        has_ranking = index < ranking_rows
        positive = index < positive_rows
        rows.append(
            {
                "ticker": f"KXTEMPNYCH-26JUL1401-T{70 + index}.99",
                "market_title": "Will the NYC temperature be above a threshold?",
                "status": "open",
                "target_time": "2026-07-14T05:00:00+00:00",
                "minutes_until_target": minutes,
                "window_role": "SELECTED_NEXT_LIVE_WINDOW",
                "has_link": True,
                "link_target_time": "2026-07-14T05:00:00+00:00",
                "link_target_matches_window": True,
                "has_snapshot": has_forecast or has_ranking,
                "snapshot_fresh": has_forecast or has_ranking,
                "latest_snapshot_at": "2026-07-14T04:05:00+00:00" if has_forecast or has_ranking else None,
                "has_source_forecast": True,
                "source_forecast_fresh": True,
                "source_forecast_at": "2026-07-14T04:04:00+00:00",
                "has_weather_feature": True,
                "weather_feature_fresh": True,
                "weather_feature_at": "2026-07-14T04:04:30+00:00",
                "has_current_forecast": has_forecast,
                "latest_forecast_at": "2026-07-14T04:06:00+00:00" if has_forecast else None,
                "has_current_ranking": has_ranking,
                "latest_ranking_at": "2026-07-14T04:07:00+00:00" if has_ranking else None,
                "estimated_edge": "0.0200" if positive else "-0.0200" if has_ranking else None,
                "opportunity_score": "65" if positive else "35" if has_ranking else None,
                "first_window_blocker": "POSITIVE_EV_READY_FOR_PAPER_GATE"
                if positive
                else "EV_NOT_POSITIVE"
                if has_ranking
                else "FORECAST_MISSING",
            }
        )
    return {
        "ok": True,
        "generated_at": "2026-07-14T04:10:00+00:00",
        "selected_target_time": "2026-07-14T05:00:00+00:00",
        "rows": rows,
        "audit": {
            "active_series_market_rows": 40,
            "future_series_market_rows": 10,
            "expired_series_market_rows": 30,
            "latest_expired_target_time": "2026-07-14T04:00:00+00:00",
        },
        "summary": {
            "selected_target_time": "2026-07-14T05:00:00+00:00",
            "selected_minutes_until_target": minutes,
            "selected_window_market_rows": 10,
            "selected_window_linked_rows": 10,
            "selected_window_missing_link_rows": 0,
            "selected_window_stale_link_rows": 0,
            "selected_window_snapshot_rows": forecast_rows,
            "selected_window_fresh_snapshot_rows": forecast_rows,
            "selected_window_source_forecast_rows": 10,
            "selected_window_feature_rows": 10,
            "selected_window_forecast_rows": forecast_rows,
            "selected_window_ranking_rows": ranking_rows,
            "selected_window_positive_ev_rows": positive_rows,
            "selected_window_non_positive_ev_rows": max(0, ranking_rows - positive_rows),
            "selected_window_too_close_to_expiry": minutes < 20,
            "first_window_blocker_counts": {"FORECAST_MISSING": max(0, 10 - forecast_rows)},
        },
    }
