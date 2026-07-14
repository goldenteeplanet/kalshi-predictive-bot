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
from kalshi_predictor.phase3bb_r55_weather_ranking_path_retry import (
    build_phase3bb_r55_weather_ranking_path_retry,
    write_phase3bb_r55_weather_ranking_path_retry_report,
)


def test_phase3bb_r55_waits_for_writer_then_runs_r51_for_live_window(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    calls: list[str] = []

    with session_factory() as session:
        artifacts = write_phase3bb_r55_weather_ranking_path_retry_report(
            session,
            output_dir=reports_dir / "phase3bb_r55",
            reports_dir=reports_dir,
            r53_output_dir=reports_dir / "phase3bb_r53",
            r51_output_dir=reports_dir / "phase3bb_r51",
            expected_writer_pid=88544,
            max_wait_seconds=1,
            poll_interval_seconds=0,
            probe_runner=_fake_probe_runner(
                calls=calls,
                writer_wait=[
                    _writer_state(safe=False, pid=88544),
                    _writer_state(safe=True, pid=None),
                ],
                r53_state=_r53_state(missing_links=0, minutes_until_target=20),
                r51_pre_state=_path_state(live_rows=10, snapshot_rows=0, forecast_rows=0, ranking_rows=0),
                r51_post_state=_path_state(live_rows=10, snapshot_rows=10, forecast_rows=10, ranking_rows=10),
            ),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["decision"]
    assert decision["status"] == "WEATHER_RANKING_PATH_RETRY_COMPLETED"
    assert decision["r51_ranking_rows"] == 10
    assert payload["r51_gate"]["allowed"] is True
    assert payload["r51_summary"]["status"] == "WEATHER_RANKING_PATH_REPAIRED"
    assert "weather_snapshot_capture" in calls
    assert "weather_forecast_run" in calls
    assert "weather_fast_lane_run" in calls
    assert "phase3bb-r52-weather-ev-fair-value-diagnostic" in decision["operator_next_command"]
    assert payload["safety_flags"]["runs_weather_forecast"] is True
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert payload["safety_flags"]["starts_or_stops_r5"] is False
    assert artifacts.r51_summary_csv_path.exists()


def test_phase3bb_r55_does_not_rerun_r51_when_writer_never_clears(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    calls: list[str] = []

    with session_factory() as session:
        payload = build_phase3bb_r55_weather_ranking_path_retry(
            session,
            output_dir=reports_dir / "phase3bb_r55",
            reports_dir=reports_dir,
            r53_output_dir=reports_dir / "phase3bb_r53",
            r51_output_dir=reports_dir / "phase3bb_r51",
            expected_writer_pid=88544,
            max_wait_seconds=1,
            poll_interval_seconds=0,
            probe_runner=_fake_probe_runner(
                calls=calls,
                writer_wait=[
                    _writer_state(safe=False, pid=88544),
                    _writer_state(safe=False, pid=88544),
                ],
                r53_state=_r53_state(missing_links=0, minutes_until_target=20),
                r51_pre_state=_path_state(live_rows=10, snapshot_rows=0, forecast_rows=0, ranking_rows=0),
                r51_post_state=_path_state(live_rows=10, snapshot_rows=10, forecast_rows=10, ranking_rows=10),
            ),
        )

    assert payload["decision"]["status"] == "WAITING_FOR_WRITER_CLEAR"
    assert payload["decision"]["first_hard_blocker"] == "ACTIVE_WRITER"
    assert payload["r53_gate_payload"] == {}
    assert payload["r51_payload"] == {}
    assert "weather_snapshot_capture" not in calls
    assert payload["safety_flags"]["runs_weather_forecast"] is False


def test_phase3bb_r55_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r55-weather-ranking-path-retry", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r55-weather-ranking-path-retry" in result.output
    assert "expected-writer-pid" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r55.db'}")
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
    calls: list[str],
    writer_wait: list[dict[str, object]],
    r53_state: dict[str, object],
    r51_pre_state: dict[str, object],
    r51_post_state: dict[str, object],
):
    writer_count = {"value": 0}

    def _runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        calls.append(probe.name)
        stdout = ""
        if probe.name.startswith("writer_gate_check"):
            index = min(writer_count["value"], len(writer_wait) - 1)
            writer_count["value"] += 1
            stdout = json.dumps(writer_wait[index])
        elif probe.name == "remote_time_utc":
            stdout = "2026-07-14T01:40:00Z\n"
        elif probe.name == "db_writer_monitor":
            stdout = json.dumps(_writer_state(safe=True, pid=None))
        elif probe.name in {"db_writer_monitor_pre", "db_writer_monitor_post"}:
            stdout = json.dumps({"status": "CLEAR", "safe_to_start_write": True, "current_writer_pid": None})
        elif probe.name == "command_registry":
            stdout = "COMMAND_REGISTRY_OK\n"
        elif probe.name == "r12_preview_json":
            stdout = json.dumps({"summary": {"rows_safe_to_link": 0, "rows_safe_to_relink": 0}})
        elif probe.name == "weather_current_window_state":
            stdout = json.dumps(r53_state)
        elif probe.name == "weather_ranking_path_state_pre":
            stdout = json.dumps(r51_pre_state)
        elif probe.name == "weather_ranking_path_state_post":
            stdout = json.dumps(r51_post_state)
        elif probe.name == "weather_snapshot_capture":
            stdout = "Captured 10 snapshots.\n"
        elif probe.name == "weather_forecast_run":
            stdout = "Scanned 10 snapshots. Inserted 10 forecasts. Skipped 0.\n"
        elif probe.name == "weather_fast_lane_run":
            stdout = "Wrote JSON: reports/phase3bb_r2/weather_funnel.json\n"
        elif probe.name == "weather_funnel_json":
            stdout = json.dumps(
                {
                    "status": "WEATHER_FAST_LANE_GAP_EXPLAINED",
                    "summary": {"current_weather_rows": 10, "ranking_rows": r51_post_state["summary"]["ranking_rows"]},
                }
            )
        elif probe.name == "weather_ranking_activation_json":
            stdout = json.dumps({"status": "WEATHER_RANKING_ACTIVATED", "summary": r51_post_state["summary"]})
        elif probe.name == "weather_ranking_path_report_stats":
            stdout = _report_stats()
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
        "current_writer_elapsed_seconds": 11520 if pid else None,
    }


def _r53_state(*, missing_links: int, minutes_until_target: int) -> dict[str, object]:
    linked = 10 - missing_links
    return {
        "ok": True,
        "generated_at": "2026-07-14T01:40:00+00:00",
        "rows": [],
        "audit": {
            "active_series_market_rows": 500,
            "future_series_market_rows": 10,
            "expired_series_market_rows": 490,
            "latest_expired_target_time": "2026-07-14T01:00:00+00:00",
        },
        "summary": {
            "selected_target_time": "2026-07-14T02:00:00+00:00",
            "selected_minutes_until_target": minutes_until_target,
            "selected_window_market_rows": 10,
            "selected_window_linked_rows": linked,
            "selected_window_missing_link_rows": missing_links,
            "selected_window_stale_link_rows": 0,
            "selected_window_snapshot_rows": 0,
            "selected_window_fresh_snapshot_rows": 0,
            "selected_window_source_forecast_rows": 10,
            "selected_window_feature_rows": 10,
            "selected_window_forecast_rows": 0,
            "selected_window_ranking_rows": 0,
            "selected_window_positive_ev_rows": 0,
            "selected_window_non_positive_ev_rows": 0,
            "selected_window_too_close_to_expiry": minutes_until_target < 10,
        },
    }


def _path_state(
    *,
    live_rows: int,
    snapshot_rows: int,
    forecast_rows: int,
    ranking_rows: int,
) -> dict[str, object]:
    blocker = "RANKING_PRESENT" if ranking_rows else "SNAPSHOT_MISSING"
    return {
        "ok": True,
        "rows": [
            {
                "ticker": f"KXTEMPNYCH-26JUL1402-T{75 + index}.99",
                "target_time": "2026-07-14T02:00:00+00:00",
                "target_window_state": "LIVE_OR_FUTURE",
                "has_snapshot": index < snapshot_rows,
                "snapshot_fresh": index < snapshot_rows,
                "has_weather_feature": index < live_rows,
                "weather_feature_fresh": index < live_rows,
                "has_current_forecast": index < forecast_rows,
                "has_current_ranking": index < ranking_rows,
                "forecast_skip_reason": None,
                "first_path_blocker": blocker,
            }
            for index in range(live_rows)
        ],
        "summary": {
            "current_weather_links": live_rows,
            "target_expired_rows": 0,
            "live_or_future_rows": live_rows,
            "snapshot_rows": snapshot_rows,
            "fresh_snapshot_rows": snapshot_rows,
            "source_forecast_rows": live_rows,
            "fresh_source_forecast_rows": live_rows,
            "weather_feature_rows": live_rows,
            "fresh_weather_feature_rows": live_rows,
            "forecast_rows": forecast_rows,
            "ranking_rows": ranking_rows,
            "first_path_blocker_counts": {blocker: live_rows},
        },
        "skip_reason_counts": {},
    }


def _report_stats() -> str:
    return "\n".join(
        [
            "reports/phase3bb_r2/weather_funnel.json|1783981040|2000",
            "reports/phase3bb_r2/weather_candidates.csv|1783981040|2000",
            "reports/phase3ba_r2/weather_ranking_activation.json|1783981040|900",
            "reports/phase3ba_r2/weather_opportunity_rows.csv|1783981040|900",
            "reports/phase3ba_r3/weather_paper_gate.json|1783981040|900",
            "reports/phase3az_r12_weather/weather_activation_preview.json|1783981040|900",
            "reports/weather_opportunities.md|1783981040|900",
        ]
    )
