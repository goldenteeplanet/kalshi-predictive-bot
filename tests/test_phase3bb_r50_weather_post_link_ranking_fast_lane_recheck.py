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
from kalshi_predictor.phase3bb_r50_weather_post_link_ranking_fast_lane_recheck import (
    build_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck,
    write_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck_report,
)


def test_phase3bb_r50_runs_fast_lane_and_finds_rankings(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    seen: list[str] = []

    with session_factory() as session:
        artifacts = write_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck_report(
            session,
            output_dir=reports_dir / "phase3bb_r50",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(seen=seen),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["fast_lane_decision"]
    assert "weather_fast_lane_run" in seen
    assert payload["phase"] == "3BB-R50-WEATHER-POST-LINK-RANKING-FAST-LANE-RECHECK"
    assert decision["status"] == "WEATHER_FAST_LANE_RANKING_PRESENT"
    assert decision["fast_lane_run_attempted"] is True
    assert decision["fast_lane_run_ok"] is True
    assert decision["current_weather_rows"] == 10
    assert decision["ranking_rows"] == 10
    assert decision["positive_ev_rows"] == 2
    assert "phase3bb-r8-unified-paper-gate" in decision["operator_next_command"]
    assert payload["safety_flags"]["runs_weather_fast_lane"] is True
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert all(row["passed"] for row in payload["fast_lane_checks"])
    assert artifacts.fast_lane_summary_csv_path.exists()


def test_phase3bb_r50_waits_when_writer_busy(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    seen: list[str] = []

    with session_factory() as session:
        payload = build_phase3bb_r50_weather_post_link_ranking_fast_lane_recheck(
            session,
            output_dir=reports_dir / "phase3bb_r50",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(
                seen=seen,
                writer_pre={"status": "WRITER_ACTIVE", "safe_to_start_write": False, "current_writer_pid": 42},
            ),
        )

    decision = payload["fast_lane_decision"]
    assert "weather_fast_lane_run" not in seen
    assert decision["status"] == "WAIT_FOR_WRITER_CLEAR"
    assert decision["fast_lane_run_attempted"] is False
    assert decision["operator_next_command"] == "kalshi-bot db-writer-monitor --json"
    assert payload["safety_flags"]["runs_weather_fast_lane"] is False


def test_phase3bb_r50_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r50-weather-post-link-ranking-fast-lane-recheck", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r50-weather-post-link-ranking-fast-lane-recheck" in result.output
    assert "--run-fast-lane" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r50.db'}")
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
    writer_pre: dict[str, object] | None = None,
    funnel_summary: dict[str, object] | None = None,
):
    writer_pre = writer_pre or {
        "status": "OPEN_READERS",
        "safe_to_start_write": True,
        "current_writer_pid": None,
    }
    funnel_summary = funnel_summary or {
        "current_weather_rows": 10,
        "verified_link_rows": 10,
        "forecast_rows": 10,
        "ranking_rows": 10,
        "positive_ev_rows": 2,
        "paper_ready_rows": 0,
        "first_hard_blocker": "EV_NOT_POSITIVE",
    }
    outputs = {
        "remote_time_utc": ("2026-07-13T23:00:00Z\n", True, 0, ""),
        "db_writer_monitor_pre": (json.dumps(writer_pre), True, 0, ""),
        "r49_json": (
            json.dumps(
                {
                    "post_link_decision": {
                        "status": "WEATHER_MISSING_LINK_APPLY_VERIFIED",
                        "verification_passed": True,
                        "rows_written": 10,
                        "rows_safe_to_link": 0,
                        "rows_safe_to_relink": 0,
                    }
                }
            ),
            True,
            0,
            "",
        ),
        "command_registry": ("COMMAND_REGISTRY_OK\n", True, 0, ""),
        "weather_fast_lane_run": (
            "Phase 3BB-R2 Weather Fast Lane\nWrote JSON: reports/phase3bb_r2/weather_funnel.json\n",
            True,
            0,
            "",
        ),
        "db_writer_monitor_post": (
            json.dumps({"status": "OPEN_READERS", "safe_to_start_write": True, "current_writer_pid": None}),
            True,
            0,
            "",
        ),
        "weather_funnel_json": (
            json.dumps({"status": "WEATHER_RANKED_GATE_BLOCKED", "summary": funnel_summary}),
            True,
            0,
            "",
        ),
        "weather_ranking_activation_json": (
            json.dumps({"status": "WEATHER_RANKING_ACTIVATED", "summary": {"ranking_rows": 10}}),
            True,
            0,
            "",
        ),
        "weather_current_window_snapshot": (
            json.dumps(
                {
                    "ok": True,
                    "summary": {
                        "current_weather_market_rows": 40,
                        "missing_current_weather_link_rows": 30,
                        "fresh_feature_window_missing_rows": 0,
                    },
                }
            ),
            True,
            0,
            "",
        ),
        "weather_ranking_recheck_report_stats": (_report_stats(), True, 0, ""),
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


def _report_stats() -> str:
    return "\n".join(
        [
            "reports/phase3bb_r49/weather_missing_link_apply_after_feature_refresh.json|1783981000|1000",
            "reports/phase3bb_r2/weather_funnel.json|1783981040|2000",
            "reports/phase3bb_r2/weather_candidates.csv|1783981040|2000",
            "reports/phase3ba_r2/weather_ranking_activation.json|1783981040|900",
            "reports/phase3ba_r2/weather_opportunity_rows.csv|1783981040|900",
            "reports/weather_opportunities.md|1783981040|900",
        ]
    )
