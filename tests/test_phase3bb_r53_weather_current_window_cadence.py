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
from kalshi_predictor.phase3bb_r53_weather_current_window_cadence import (
    build_phase3bb_r53_weather_current_window_cadence,
    write_phase3bb_r53_weather_current_window_cadence_report,
)


def test_phase3bb_r53_routes_newest_window_missing_links_to_r12_apply(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r53_weather_current_window_cadence_report(
            session,
            output_dir=reports_dir / "phase3bb_r53",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(_window_state(blocker="MISSING_WEATHER_LINK")),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["decision"]
    assert decision["status"] == "WEATHER_CURRENT_WINDOW_LINK_APPLY_NEEDED"
    assert decision["first_hard_blocker"] == "MISSING_WEATHER_LINK"
    assert decision["blocked_by_writer"] is False
    assert "phase3az-r12-weather-missing-link-apply" in decision["operator_next_command"]
    assert payload["summary"]["selected_window_missing_link_rows"] == 10
    assert payload["safety_flags"]["ssh_write_capable_commands_executed"] == 0
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert artifacts.rows_csv_path.exists()


def test_phase3bb_r53_routes_ranked_newest_window_to_ev_diagnostic(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r53_weather_current_window_cadence(
            session,
            output_dir=reports_dir / "phase3bb_r53",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(_window_state(blocker="EV_NOT_POSITIVE")),
        )

    decision = payload["decision"]
    assert decision["status"] == "WEATHER_CURRENT_WINDOW_EV_NOT_POSITIVE"
    assert decision["first_hard_blocker"] == "EV_NOT_POSITIVE"
    assert "phase3bb-r52-weather-ev-fair-value-diagnostic" in decision["operator_next_command"]
    assert payload["summary"]["selected_window_ranking_rows"] == 10
    assert payload["summary"]["selected_window_positive_ev_rows"] == 0
    assert payload["paper_trade_creation"] is False


def test_phase3bb_r53_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair" in result.output
    assert "min-minutes-before-target" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r53.db'}")
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


def _fake_probe_runner(state: dict[str, object], *, writer_safe: bool = True):
    outputs = {
        "remote_time_utc": ("2026-07-14T00:10:00Z\n", True, 0, ""),
        "db_writer_monitor": (
            json.dumps(
                {
                    "status": "CLEAR" if writer_safe else "WRITER_ACTIVE",
                    "safe_to_start_write": writer_safe,
                    "current_writer_pid": None if writer_safe else 123,
                }
            ),
            True,
            0,
            "",
        ),
        "command_registry": ("COMMAND_REGISTRY_OK\n", True, 0, ""),
        "r12_preview_json": (
            json.dumps(
                {
                    "summary": {
                        "rows_safe_to_link": 0,
                        "rows_safe_to_relink": 0,
                        "first_blocker": "TARGET_TIME_NOT_CURRENT",
                    }
                }
            ),
            True,
            0,
            "",
        ),
        "weather_current_window_state": (json.dumps(state), True, 0, ""),
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


def _window_state(*, blocker: str) -> dict[str, object]:
    missing = blocker == "MISSING_WEATHER_LINK"
    ranked = blocker == "EV_NOT_POSITIVE"
    rows = [
        {
            "ticker": f"KXTEMPNYCH-26JUL1401-T{70 + index}.99",
            "market_title": "Will the NYC temperature be above a threshold?",
            "status": "open",
            "target_time": "2026-07-14T01:00:00+00:00",
            "minutes_until_target": 50,
            "window_role": "SELECTED_NEXT_LIVE_WINDOW",
            "has_link": not missing,
            "link_target_time": "2026-07-14T01:00:00+00:00" if not missing else None,
            "link_target_matches_window": not missing,
            "has_snapshot": ranked,
            "snapshot_fresh": ranked,
            "latest_snapshot_at": "2026-07-14T00:05:00+00:00" if ranked else None,
            "has_source_forecast": ranked,
            "source_forecast_fresh": ranked,
            "source_forecast_at": "2026-07-14T00:04:00+00:00" if ranked else None,
            "has_weather_feature": ranked,
            "weather_feature_fresh": ranked,
            "weather_feature_at": "2026-07-14T00:04:30+00:00" if ranked else None,
            "has_current_forecast": ranked,
            "latest_forecast_at": "2026-07-14T00:06:00+00:00" if ranked else None,
            "has_current_ranking": ranked,
            "latest_ranking_at": "2026-07-14T00:07:00+00:00" if ranked else None,
            "estimated_edge": "-0.0200" if ranked else None,
            "opportunity_score": "35" if ranked else None,
            "first_window_blocker": blocker,
        }
        for index in range(10)
    ]
    return {
        "ok": True,
        "generated_at": "2026-07-14T00:10:00+00:00",
        "selected_target_time": "2026-07-14T01:00:00+00:00",
        "rows": rows,
        "audit": {
            "active_series_market_rows": 40,
            "future_series_market_rows": 10,
            "expired_series_market_rows": 30,
            "latest_expired_target_time": "2026-07-14T00:00:00+00:00",
        },
        "summary": {
            "selected_target_time": "2026-07-14T01:00:00+00:00",
            "selected_minutes_until_target": 50,
            "selected_window_market_rows": 10,
            "selected_window_linked_rows": 0 if missing else 10,
            "selected_window_missing_link_rows": 10 if missing else 0,
            "selected_window_stale_link_rows": 0,
            "selected_window_snapshot_rows": 10 if ranked else 0,
            "selected_window_fresh_snapshot_rows": 10 if ranked else 0,
            "selected_window_source_forecast_rows": 10 if ranked else 0,
            "selected_window_feature_rows": 10 if ranked else 0,
            "selected_window_forecast_rows": 10 if ranked else 0,
            "selected_window_ranking_rows": 10 if ranked else 0,
            "selected_window_positive_ev_rows": 0,
            "selected_window_non_positive_ev_rows": 10 if ranked else 0,
            "selected_window_too_close_to_expiry": False,
            "first_window_blocker_counts": {blocker: 10},
        },
    }
