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
from kalshi_predictor.phase3bb_r54_weather_missing_link_apply_deferral import (
    build_phase3bb_r54_weather_missing_link_apply_deferral,
    write_phase3bb_r54_weather_missing_link_apply_deferral_report,
)


def test_phase3bb_r54_waits_for_expected_writer_then_applies_missing_links(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    calls: list[str] = []

    with session_factory() as session:
        artifacts = write_phase3bb_r54_weather_missing_link_apply_deferral_report(
            session,
            output_dir=reports_dir / "phase3bb_r54",
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
                    _r53_state(missing_links=10),
                    _r53_state(missing_links=0),
                ],
            ),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["decision"]
    assert decision["status"] == "WEATHER_MISSING_LINK_APPLY_COMPLETED"
    assert decision["link_rows_written"] == 10
    assert decision["post_apply_missing_links"] == 0
    assert "r12_missing_link_apply" in calls
    assert payload["writer_wait"]["attempt_count"] == 2
    assert payload["safety_flags"]["runs_missing_link_apply"] is True
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert artifacts.wait_checks_csv_path.exists()


def test_phase3bb_r54_does_not_apply_when_writer_does_not_clear(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    calls: list[str] = []

    with session_factory() as session:
        payload = build_phase3bb_r54_weather_missing_link_apply_deferral(
            session,
            output_dir=reports_dir / "phase3bb_r54",
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
                r53_states=[_r53_state(missing_links=10)],
            ),
        )

    decision = payload["decision"]
    assert decision["status"] == "WAITING_FOR_WRITER_CLEAR"
    assert decision["first_hard_blocker"] == "ACTIVE_WRITER"
    assert "r12_missing_link_apply" not in calls
    assert payload["safety_flags"]["runs_missing_link_apply"] is False
    assert payload["r53_gate_payload"] == {}


def test_phase3bb_r54_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r54-weather-missing-link-apply-deferral", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r54-weather-missing-link-apply-deferral" in result.output
    assert "expected-writer-pid" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r54.db'}")
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
    r53_states: list[dict[str, object]],
):
    writer_count = {"value": 0}
    r53_count = {"value": 0}

    def _runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        calls.append(probe.name)
        if probe.name.startswith("writer_gate_check"):
            index = min(writer_count["value"], len(writer_wait) - 1)
            writer_count["value"] += 1
            stdout = json.dumps(writer_wait[index])
        elif probe.name == "remote_time_utc":
            stdout = "2026-07-14T01:40:00Z\n"
        elif probe.name == "db_writer_monitor":
            stdout = json.dumps(_writer_state(safe=True, pid=None))
        elif probe.name == "command_registry":
            stdout = "COMMAND_REGISTRY_OK\n"
        elif probe.name == "r12_preview_json":
            stdout = json.dumps(
                {
                    "summary": {
                        "rows_safe_to_link": 10,
                        "rows_safe_to_relink": 0,
                        "first_blocker": "SAFE_TO_LINK",
                    }
                }
            )
        elif probe.name == "weather_current_window_state":
            index = min(r53_count["value"], len(r53_states) - 1)
            r53_count["value"] += 1
            stdout = json.dumps(r53_states[index])
        elif probe.name == "r12_missing_link_apply":
            stdout = json.dumps(
                {
                    "status": "APPLIED",
                    "summary": {
                        "preview_rows_safe_to_link": 10,
                        "candidates_reviewed": 10,
                        "would_write_link_rows": 0,
                        "link_rows_written": 10,
                        "skipped_rows": 0,
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


def _writer_state(*, safe: bool, pid: int | None) -> dict[str, object]:
    return {
        "status": "OPEN_READERS" if safe else "WRITER_ACTIVE",
        "safe_to_start_write": safe,
        "current_writer_pid": pid,
        "current_writer_elapsed_seconds": 11520 if pid else None,
    }


def _r53_state(*, missing_links: int) -> dict[str, object]:
    linked = 10 - missing_links
    return {
        "ok": True,
        "generated_at": "2026-07-14T01:40:00+00:00",
        "selected_target_time": "2026-07-14T02:00:00+00:00",
        "rows": [],
        "audit": {
            "active_series_market_rows": 500,
            "future_series_market_rows": 10,
            "expired_series_market_rows": 490,
            "latest_expired_target_time": "2026-07-14T01:00:00+00:00",
        },
        "summary": {
            "selected_target_time": "2026-07-14T02:00:00+00:00",
            "selected_minutes_until_target": 20,
            "selected_window_market_rows": 10,
            "selected_window_linked_rows": linked,
            "selected_window_missing_link_rows": missing_links,
            "selected_window_stale_link_rows": 0,
            "selected_window_snapshot_rows": 0,
            "selected_window_fresh_snapshot_rows": 0,
            "selected_window_source_forecast_rows": 0,
            "selected_window_feature_rows": 0,
            "selected_window_forecast_rows": 0,
            "selected_window_ranking_rows": 0,
            "selected_window_positive_ev_rows": 0,
            "selected_window_non_positive_ev_rows": 0,
            "selected_window_too_close_to_expiry": False,
            "first_window_blocker_counts": {"MISSING_WEATHER_LINK": missing_links}
            if missing_links
            else {},
        },
    }
