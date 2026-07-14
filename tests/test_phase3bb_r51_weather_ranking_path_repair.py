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
from kalshi_predictor.phase3bb_r51_weather_ranking_path_repair import (
    build_phase3bb_r51_weather_ranking_path_repair,
    write_phase3bb_r51_weather_ranking_path_repair_report,
)


def test_phase3bb_r51_repairs_live_weather_ranking_path(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    seen: list[str] = []

    with session_factory() as session:
        artifacts = write_phase3bb_r51_weather_ranking_path_repair_report(
            session,
            output_dir=reports_dir / "phase3bb_r51",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(
                seen=seen,
                pre_state=_path_state(live_rows=10, snapshot_rows=0, forecast_rows=0, ranking_rows=0),
                post_state=_path_state(live_rows=10, snapshot_rows=10, forecast_rows=10, ranking_rows=10),
            ),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["ranking_path_decision"]
    assert "weather_snapshot_capture" in seen
    assert "weather_forecast_run" in seen
    assert "weather_fast_lane_run" in seen
    assert decision["status"] == "WEATHER_RANKING_PATH_REPAIRED"
    assert decision["repair_run_attempted"] is True
    assert decision["snapshot_capture_ok"] is True
    assert decision["forecast_run_ok"] is True
    assert decision["fast_lane_run_ok"] is True
    assert decision["ranking_rows"] == 10
    assert "phase3bb-r8-unified-paper-gate" in decision["operator_next_command"]
    assert payload["safety_flags"]["runs_missing_link_apply"] is False
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert all(row["passed"] for row in payload["ranking_path_checks"])
    assert artifacts.path_rows_csv_path.exists()


def test_phase3bb_r51_skips_repair_for_expired_target_windows(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    seen: list[str] = []

    with session_factory() as session:
        payload = build_phase3bb_r51_weather_ranking_path_repair(
            session,
            output_dir=reports_dir / "phase3bb_r51",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(
                seen=seen,
                pre_state=_path_state(live_rows=0, expired_rows=10, snapshot_rows=0, forecast_rows=0, ranking_rows=0),
                post_state=_path_state(live_rows=0, expired_rows=10, snapshot_rows=0, forecast_rows=0, ranking_rows=0),
                writer_pre={"status": "WRITER_ACTIVE", "safe_to_start_write": False, "current_writer_pid": 123},
            ),
        )

    decision = payload["ranking_path_decision"]
    assert "weather_snapshot_capture" not in seen
    assert "weather_forecast_run" not in seen
    assert decision["status"] == "WEATHER_RANKING_PATH_TARGET_WINDOW_EXPIRED"
    assert decision["first_weather_path_blocker"] == "EXPIRED_TARGET_WINDOW"
    assert "phase3bb-r47-weather-current-window-series-discovery-linkability-repair" in decision["operator_next_command"]
    assert payload["safety_flags"]["runs_weather_forecast"] is False


def test_phase3bb_r51_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r51-weather-ranking-path-repair", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r51-weather-ranking-path-repair" in result.output
    assert "--run-repair" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r51.db'}")
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
    seen: list[str],
    pre_state: dict[str, object],
    post_state: dict[str, object],
    writer_pre: dict[str, object] | None = None,
):
    writer_pre = writer_pre or {
        "status": "CLEAR",
        "safe_to_start_write": True,
        "current_writer_pid": None,
    }
    outputs = {
        "remote_time_utc": ("2026-07-13T23:00:00Z\n", True, 0, ""),
        "db_writer_monitor_pre": (json.dumps(writer_pre), True, 0, ""),
        "command_registry": ("COMMAND_REGISTRY_OK\n", True, 0, ""),
        "weather_ranking_path_state_pre": (json.dumps(pre_state), True, 0, ""),
        "weather_snapshot_capture": ("Captured 10 snapshots.\n", True, 0, ""),
        "weather_forecast_run": ("Scanned 10 snapshots. Inserted 10 forecasts. Skipped 0.\n", True, 0, ""),
        "weather_fast_lane_run": ("Wrote JSON: reports/phase3bb_r2/weather_funnel.json\n", True, 0, ""),
        "db_writer_monitor_post": (
            json.dumps({"status": "CLEAR", "safe_to_start_write": True, "current_writer_pid": None}),
            True,
            0,
            "",
        ),
        "weather_ranking_path_state_post": (json.dumps(post_state), True, 0, ""),
        "weather_funnel_json": (
            json.dumps(
                {
                    "status": "WEATHER_FAST_LANE_GAP_EXPLAINED",
                    "summary": {"current_weather_rows": 10, "ranking_rows": post_state["summary"]["ranking_rows"]},
                }
            ),
            True,
            0,
            "",
        ),
        "weather_ranking_activation_json": (
            json.dumps({"status": "WEATHER_RANKING_ACTIVATED", "summary": post_state["summary"]}),
            True,
            0,
            "",
        ),
        "weather_ranking_path_report_stats": (_report_stats(), True, 0, ""),
    }

    def _runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        seen.append(probe.name)
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


def _path_state(
    *,
    live_rows: int,
    snapshot_rows: int,
    forecast_rows: int,
    ranking_rows: int,
    expired_rows: int = 0,
) -> dict[str, object]:
    total = live_rows + expired_rows
    blocker = "RANKING_PRESENT" if ranking_rows else ("EXPIRED_TARGET_WINDOW" if expired_rows else "SNAPSHOT_MISSING")
    return {
        "ok": True,
        "rows": [
            {
                "ticker": f"KXTEMPNYCH-26JUL1319-T{75 + index}.99",
                "target_time": "2026-07-13T23:00:00+00:00",
                "target_window_state": "EXPIRED" if index < expired_rows else "LIVE_OR_FUTURE",
                "has_snapshot": index < snapshot_rows,
                "snapshot_fresh": index < snapshot_rows,
                "has_weather_feature": index < live_rows,
                "weather_feature_fresh": index < live_rows,
                "has_current_forecast": index < forecast_rows,
                "has_current_ranking": index < ranking_rows,
                "forecast_skip_reason": None,
                "first_path_blocker": blocker,
            }
            for index in range(total)
        ],
        "summary": {
            "current_weather_links": total,
            "target_expired_rows": expired_rows,
            "live_or_future_rows": live_rows,
            "snapshot_rows": snapshot_rows,
            "fresh_snapshot_rows": snapshot_rows,
            "source_forecast_rows": live_rows,
            "fresh_source_forecast_rows": live_rows,
            "weather_feature_rows": live_rows,
            "fresh_weather_feature_rows": live_rows,
            "forecast_rows": forecast_rows,
            "ranking_rows": ranking_rows,
            "first_path_blocker_counts": {blocker: total},
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
