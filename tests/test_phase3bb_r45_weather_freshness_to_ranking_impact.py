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
from kalshi_predictor.phase3bb_r45_weather_freshness_to_ranking_impact import (
    build_phase3bb_r45_weather_freshness_to_ranking_impact,
    write_phase3bb_r45_weather_freshness_to_ranking_impact_report,
)


def test_phase3bb_r45_reports_refresh_without_rankable_current_rows(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r45_weather_freshness_to_ranking_impact_report(
            session,
            output_dir=reports_dir / "phase3bb_r45",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["impact_decision"]
    parsed = payload["parsed_impact_state"]
    assert payload["phase"] == "3BB-R45-WEATHER-FRESHNESS-TO-RANKING-IMPACT-REVIEW"
    assert decision["status"] == "WEATHER_REFRESH_DID_NOT_CREATE_RANKABLE_CURRENT_LINKS"
    assert decision["first_weather_blocker"] == "NO_CURRENT_WEATHER_LINKS_AFTER_CATALOG_REFRESH"
    assert decision["rows_safe_to_link"] == 0
    assert decision["current_linkable_weather_tickers"] == 10
    assert decision["current_weather_rows"] == 0
    assert decision["db_current_weather_links"] == 0
    assert "Phase 3BB-R47" in decision["next_codex_step"]
    assert parsed["weather_activation_blocker_counts"]["TARGET_TIME_NOT_CURRENT"] == 34
    assert artifacts.blocker_counts_csv_path.exists()
    assert artifacts.freshness_rows_csv_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3bb_r45_prioritizes_safe_link_gate_when_rows_exist(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    runner = _fake_probe_runner(
        weather_preview=json.dumps(
            {
                "summary": {
                    "active_weather_markets_reviewed": 64,
                    "current_linkable_weather_tickers": 10,
                    "rows_safe_to_link": 10,
                    "rows_safe_to_relink": 0,
                    "first_blocker": "SAFE_TO_LINK",
                },
                "candidate_rows": [
                    {
                        "ticker": "KXTEMPNYCH-26JUL1317-T75",
                        "blocker": "SAFE_TO_LINK",
                        "safe_to_link": True,
                        "safe_to_relink": False,
                        "current_linkable_weather_ticker": True,
                        "has_existing_link": False,
                    }
                ],
            }
        )
    )

    with session_factory() as session:
        payload = build_phase3bb_r45_weather_freshness_to_ranking_impact(
            session,
            output_dir=reports_dir / "phase3bb_r45",
            reports_dir=reports_dir,
            probe_runner=runner,
        )

    decision = payload["impact_decision"]
    assert decision["status"] == "WEATHER_LINK_GATE_READY"
    assert decision["first_weather_blocker"] == "SAFE_LINK_WRITE_GATE_READY"
    assert "phase3az-r12-weather-missing-link-apply" in decision["operator_next_command"]
    assert decision["will_create_paper_trades"] is False
    assert payload["safety_flags"]["remote_db_writes_performed"] == 0


def test_phase3bb_r45_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r45-weather-freshness-to-ranking-impact", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r45-weather-freshness-to-ranking-impact" in result.output
    assert "--per-probe-timeout-seconds" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r45.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
    r11_dir = reports_dir / "phase3bb_r11"
    r11_dir.mkdir(parents=True, exist_ok=True)
    (r11_dir / "codex_cloud_context.json").write_text(
        json.dumps(
            {
                "ssh_profile": {
                    "host": "203.0.113.10",
                    "user": "kalshi",
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
    weather_preview: str | None = None,
    weather_funnel: str | None = None,
    db_snapshot: str | None = None,
    overrides: dict[str, tuple[str, bool, int | None, str]] | None = None,
):
    weather_preview = weather_preview or json.dumps(
        {
            "summary": {
                "active_weather_markets_reviewed": 64,
                "stale_target_time_links": 34,
                "current_linkable_weather_tickers": 10,
                "rows_safe_to_link": 0,
                "rows_safe_to_relink": 0,
                "missing_weather_links": 20,
                "expired_target_rows": 34,
                "first_blocker": "TARGET_TIME_NOT_CURRENT",
            },
            "candidate_rows": [
                {"ticker": f"KXTEMPNYCH-26JUL0113-T{index}", "blocker": "TARGET_TIME_NOT_CURRENT"}
                for index in range(34)
            ]
            + [
                {
                    "ticker": f"KXTEMPNYCH-26JUL1317-T{index}",
                    "blocker": "CURRENT_LINK_NOT_STALE",
                    "current_linkable_weather_ticker": True,
                    "has_existing_link": True,
                }
                for index in range(10)
            ],
        }
    )
    weather_funnel = weather_funnel or json.dumps(
        {
            "status": "NO_CURRENT_WEATHER_ROWS",
            "summary": {"current_weather_rows": 0, "ranking_rows": 0, "paper_ready_rows": 0},
        }
    )
    db_snapshot = db_snapshot or json.dumps(
        {
            "ok": True,
            "weather_market_links": {
                "rows_sampled": 20,
                "target_time_ge_now_minus_3h": 0,
                "target_time_ge_now": 0,
                "min_target_time": "2026-07-01T17:00:00+00:00",
                "max_target_time": "2026-07-01T17:00:00+00:00",
                "dominant_location_key": "new_york",
                "sample_rows": [],
            },
            "weather_features": {
                "rows_sampled": 10,
                "target_time_ge_now_minus_3h": 10,
                "generated_at_ge_now_minus_24h": 10,
                "max_generated_at": "2026-07-13T15:45:00+00:00",
                "dominant_location_key": "new_york",
                "sample_rows": [],
            },
        }
    )
    r44_json = json.dumps(
        {
            "hook_runtime_decision": {
                "status": "WEATHER_CATALOG_HOOK_RUNTIME_VERIFIED",
                "verification_passed": True,
                "weather_catalog_hook_run_count": 5,
                "weather_fast_lane_run_count": 6,
                "weather_catalog_sequence": "CATALOG_THEN_FAST_LANE_VERIFIED",
                "weather_funnel_status": "NO_CURRENT_WEATHER_ROWS",
            },
            "parsed_hook_runtime_state": {
                "scheduler_timer_active_state": "active",
                "scheduler_service_active_state": "inactive",
            },
        }
    )
    r40_json = json.dumps(
        {
            "runtime_decision": {"status": "OVERNIGHT_READY_WITH_WRITER_GATE_WARNINGS"},
            "parsed_runtime_state": {
                "weather_catalog_hook_job_run_count": 3,
                "weather_fast_lane_job_run_count": 2,
                "weather_catalog_runtime_order_ok": True,
            },
        }
    )
    outputs = {
        "remote_time_utc": ("2026-07-13T15:58:00Z\n", True, 0, ""),
        "scheduler_timer_active": ("active\n", True, 0, ""),
        "scheduler_service_active": ("inactive\n", True, 0, ""),
        "db_writer_monitor_raw": (
            json.dumps({"status": "OPEN_READERS", "safe_to_start_write": True}),
            True,
            0,
            "",
        ),
        "weather_report_stats": (_report_stats(), True, 0, ""),
        "weather_activation_preview_json": (weather_preview, True, 0, ""),
        "weather_funnel_json": (weather_funnel, True, 0, ""),
        "r44_json": (r44_json, True, 0, ""),
        "r40_json": (r40_json, True, 0, ""),
        "weather_db_snapshot": (db_snapshot, True, 0, ""),
        "command_registry": ("COMMAND_REGISTRY_OK\n", True, 0, ""),
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


def _report_stats() -> str:
    return "\n".join(
        f"{path}|1783948981|100"
        for path in [
            "reports/phase3az_r12_weather/weather_activation_preview.json",
            "reports/phase3az_r12_weather/weather_activation_preview.md",
            "reports/phase3az_r12_weather/weather_activation_candidates.csv",
            "reports/phase3az_r12_weather/safe_to_link.csv",
            "reports/phase3az_r12_weather/safe_to_relink.csv",
            "reports/phase3bb_r2/weather_funnel.json",
            "reports/phase3bb_r2/weather_fast_lane.md",
            "reports/phase3bb_r2/weather_candidates.csv",
            "reports/phase3bb_r44/weather_catalog_hook_runtime_verification.json",
            "reports/phase3bb_r40/cloud_scheduler_runtime_monitor.json",
        ]
    )
