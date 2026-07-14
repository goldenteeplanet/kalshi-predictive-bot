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
from kalshi_predictor.phase3bb_r42_weather_fast_lane_post_unblock import (
    build_phase3bb_r42_weather_fast_lane_post_unblock,
    write_phase3bb_r42_weather_fast_lane_post_unblock_report,
)


def test_phase3bb_r42_verifies_weather_fast_lane_after_r41(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    _write_r41(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r42_weather_fast_lane_post_unblock_report(
            session,
            output_dir=reports_dir / "phase3bb_r42",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["post_unblock_decision"]
    assert payload["phase"] == "3BB-R42-WEATHER-FAST-LANE-POST-UNBLOCK"
    assert decision["status"] == "WEATHER_FAST_LANE_POST_UNBLOCK_VERIFIED"
    assert decision["verification_passed"] is True
    assert decision["weather_fast_lane_run_count"] == 1
    assert decision["weather_funnel_report_refreshed_after_r41"] is True
    assert decision["will_create_paper_trades"] is False
    assert decision["will_submit_live_or_demo_orders"] is False
    assert artifacts.manifest_path.exists()


def test_phase3bb_r42_waits_when_weather_cadence_has_not_run(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    _write_r41(reports_dir)
    runner = _fake_probe_runner(
        {
            "scheduler_journal_post_unblock": ("", True, 0, ""),
            "weather_report_stats": (_report_stats(mtime=1783920000), True, 0, ""),
        }
    )

    with session_factory() as session:
        payload = build_phase3bb_r42_weather_fast_lane_post_unblock(
            session,
            output_dir=reports_dir / "phase3bb_r42",
            reports_dir=reports_dir,
            probe_runner=runner,
        )

    decision = payload["post_unblock_decision"]
    assert decision["status"] == "WAITING_FOR_NEXT_WEATHER_FAST_LANE_CYCLE"
    assert decision["verification_passed"] is False
    assert decision["weather_fast_lane_run_count"] == 0


def test_phase3bb_r42_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r42-weather-fast-lane-post-unblock-verification", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r42-weather-fast-lane-post-unblock-verification" in result.output
    assert "--journal-lines" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r42.db'}")
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


def _write_r41(reports_dir: Path) -> None:
    r41_dir = reports_dir / "phase3bb_r41"
    r41_dir.mkdir(parents=True, exist_ok=True)
    (r41_dir / "writer_gate_normalization.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-07-13T13:10:45+00:00",
                "writer_gate_decision": {
                    "status": "WRITER_GATE_NORMALIZED_WEATHER_FAST_LANE_UNBLOCKED",
                    "weather_fast_lane_unblocked": True,
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_probe_runner(
    overrides: dict[str, tuple[str, bool, int | None, str]] | None = None,
):
    journal = "\n".join(
        [
            "Jul 13 13:22:43 kalshi-bot-01 runner[1]: [phase3bb-r35] running weather_fast_lane",
            "Jul 13 13:23:01 kalshi-bot-01 runner[1]: Phase 3BB-R2 Weather Fast Lane",
            "Jul 13 13:23:01 kalshi-bot-01 runner[1]: Wrote JSON: reports/phase3bb_r2/weather_funnel.json",
        ]
    )
    weather_funnel = json.dumps(
        {
            "generated_at": "2026-07-13T13:23:01+00:00",
            "status": "NO_CURRENT_WEATHER_ROWS",
            "summary": {
                "current_weather_rows": 0,
                "ranking_rows": 0,
                "positive_ev_rows": 0,
                "paper_ready_rows": 0,
                "first_hard_blocker": "NO_CURRENT_WEATHER_ROWS",
            },
        }
    )
    outputs = {
        "remote_time_utc": ("2026-07-13T13:24:00Z\n", True, 0, ""),
        "db_writer_monitor_raw": (
            json.dumps({"status": "OPEN_READERS", "safe_to_start_write": True, "current_writer_pid": None}),
            True,
            0,
            "",
        ),
        "db_writer_monitor_json_tool": ("", True, 0, ""),
        "scheduler_timer_active": ("active\n", True, 0, ""),
        "scheduler_service_active": ("inactive\n", True, 0, ""),
        "r5_service_active": ("active\n", True, 0, ""),
        "ui_service_active": ("active\n", True, 0, ""),
        "scheduler_timer_list": (
            "NEXT LEFT LAST PASSED UNIT ACTIVATES\n"
            "Mon 2026-07-13 13:37:43 UTC 13min Mon 2026-07-13 13:22:43 UTC 1min ago kalshi-multicategory-refresh-scheduler.timer kalshi-multicategory-refresh-scheduler.service\n",
            True,
            0,
            "",
        ),
        "scheduler_journal_post_unblock": (journal, True, 0, ""),
        "weather_report_stats": (_report_stats(mtime=1783948981), True, 0, ""),
        "weather_funnel_json": (weather_funnel, True, 0, ""),
        "weather_ranking_activation_json": (json.dumps({"summary": {"ranking_rows": 0}}), True, 0, ""),
        "weather_paper_gate_json": (
            json.dumps({"summary": {"paper_ready_rows": 0, "first_hard_blocker": "NO_CURRENT_WEATHER_ROWS"}}),
            True,
            0,
            "",
        ),
        "weather_fast_lane_help": ("", True, 0, ""),
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


def _report_stats(*, mtime: int) -> str:
    paths = [
        "reports/phase3bb_r2/weather_funnel.json",
        "reports/phase3bb_r2/weather_fast_lane.md",
        "reports/phase3bb_r2/weather_candidates.csv",
        "reports/weather_opportunities.md",
        "reports/phase3ba_r2/weather_ranking_activation.json",
        "reports/phase3ba_r3/weather_paper_gate.json",
        "reports/phase3ba_r5/paper_ready_truth.json",
    ]
    return "\n".join(f"{path}|{mtime}|100" for path in paths)
