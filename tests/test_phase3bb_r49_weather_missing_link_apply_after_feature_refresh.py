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
from kalshi_predictor.phase3bb_r49_weather_missing_link_apply_after_feature_refresh import (
    build_phase3bb_r49_weather_missing_link_apply_after_feature_refresh,
    write_phase3bb_r49_weather_missing_link_apply_after_feature_refresh_report,
)


def test_phase3bb_r49_verifies_apply_and_closed_gate(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    _write_local_r48(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r49_weather_missing_link_apply_after_feature_refresh_report(
            session,
            output_dir=reports_dir / "phase3bb_r49",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(r48_payload={}),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["post_link_decision"]
    assert payload["phase"] == "3BB-R49-WEATHER-MISSING-LINK-APPLY-AFTER-FEATURE-REFRESH"
    assert decision["status"] == "WEATHER_MISSING_LINK_APPLY_VERIFIED"
    assert decision["rows_written"] == 10
    assert decision["rows_safe_to_link"] == 0
    assert decision["rows_safe_to_relink"] == 0
    assert "phase3bb-r50-weather-post-link-ranking-fast-lane-recheck" in decision["operator_next_command"]
    assert payload["safety_flags"]["remote_db_writes_performed_by_this_phase"] == 0
    assert payload["safety_flags"]["runs_weather_missing_link_apply"] is False
    assert all(row["passed"] for row in payload["post_link_checks"])
    assert artifacts.apply_summary_csv_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3bb_r49_reports_open_link_gate_without_applying(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r49_weather_missing_link_apply_after_feature_refresh(
            session,
            output_dir=reports_dir / "phase3bb_r49",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(
                apply_payload={
                    "status": "NO_SAFE_ROWS",
                    "generated_at": "2026-07-13T22:20:00+00:00",
                    "summary": {
                        "preview_rows_safe_to_link": 0,
                        "candidates_reviewed": 0,
                        "link_rows_written": 0,
                        "skipped_rows": 0,
                    },
                },
                preview_summary={
                    "active_weather_markets_reviewed": 144,
                    "rows_safe_to_link": 4,
                    "rows_safe_to_relink": 0,
                    "current_linkable_weather_tickers": 4,
                    "missing_weather_links": 4,
                    "first_blocker": "SAFE_TO_LINK",
                },
            ),
        )

    decision = payload["post_link_decision"]
    assert decision["status"] == "WEATHER_LINK_GATE_STILL_OPEN"
    assert decision["verification_passed"] is False
    assert decision["first_failed_check"] == "post_apply_link_gate_closed"
    assert "phase3az-r12-weather-missing-link-apply" in decision["operator_next_command"]
    assert payload["safety_flags"]["runs_weather_missing_link_apply"] is False


def test_phase3bb_r49_verifies_apply_but_waits_for_writer(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r49_weather_missing_link_apply_after_feature_refresh(
            session,
            output_dir=reports_dir / "phase3bb_r49",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(
                writer_payload={
                    "status": "WRITER_ACTIVE",
                    "safe_to_start_write": False,
                    "current_writer_pid": 12345,
                }
            ),
        )

    decision = payload["post_link_decision"]
    assert decision["status"] == "WEATHER_MISSING_LINK_APPLY_VERIFIED_WAIT_FOR_WRITER"
    assert decision["verification_passed"] is True
    assert decision["rows_written"] == 10
    assert decision["rows_safe_to_link"] == 0
    assert decision["operator_next_command"] == "kalshi-bot db-writer-monitor --json"


def test_phase3bb_r49_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r49-weather-missing-link-apply-after-feature-refresh", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r49-weather-missing-link-apply-after-feature-refresh" in result.output
    assert "--per-probe-timeout-seconds" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r49.db'}")
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


def _write_local_r48(reports_dir: Path) -> None:
    r48_dir = reports_dir / "phase3bb_r48"
    r48_dir.mkdir(parents=True, exist_ok=True)
    (r48_dir / "weather_feature_refresh_runtime_verification.json").write_text(
        json.dumps(
            {
                "runtime_decision": {
                    "status": "WEATHER_FEATURE_REFRESH_RUNTIME_VERIFIED_LINK_GATE_READY",
                    "rows_safe_to_link": 10,
                    "rows_safe_to_relink": 0,
                }
            }
        ),
        encoding="utf-8",
    )


def _fake_probe_runner(
    *,
    apply_payload: dict[str, object] | None = None,
    preview_summary: dict[str, object] | None = None,
    writer_payload: dict[str, object] | None = None,
    r48_payload: dict[str, object] | None = None,
):
    apply_payload = apply_payload or {
        "status": "APPLIED",
        "generated_at": "2026-07-13T22:14:04+00:00",
        "backup": "reports/phase3az_r12_weather/backups/phase3az_r12_weather_missing_link_20260713_221404.json",
        "summary": {
            "preview_rows_safe_to_link": 10,
            "candidates_reviewed": 10,
            "would_write_link_rows": 0,
            "link_rows_written": 10,
            "skipped_rows": 0,
        },
    }
    preview_summary = preview_summary or {
        "active_weather_markets_reviewed": 144,
        "rows_safe_to_link": 0,
        "rows_safe_to_relink": 0,
        "current_linkable_weather_tickers": 10,
        "missing_weather_links": 20,
        "first_blocker": "TARGET_TIME_NOT_CURRENT",
    }
    writer_payload = writer_payload or {
        "status": "OPEN_READERS",
        "safe_to_start_write": True,
        "current_writer_pid": None,
    }
    if r48_payload is None:
        r48_payload = {
            "runtime_decision": {
                "status": "WEATHER_FEATURE_REFRESH_RUNTIME_VERIFIED_LINK_GATE_READY",
                "rows_safe_to_link": 10,
                "rows_safe_to_relink": 0,
            }
        }
    outputs = {
        "remote_time_utc": ("2026-07-13T22:15:00Z\n", True, 0, ""),
        "db_writer_monitor_raw": (json.dumps(writer_payload), True, 0, ""),
        "r48_json": (json.dumps(r48_payload), True, 0, ""),
        "weather_missing_link_apply_json": (json.dumps(apply_payload), True, 0, ""),
        "weather_activation_preview_json": (json.dumps({"summary": preview_summary}), True, 0, ""),
        "weather_funnel_json": (
            json.dumps(
                {
                    "status": "NO_CURRENT_WEATHER_RANKINGS",
                    "summary": {"current_weather_rows": 0, "ranking_rows": 0},
                }
            ),
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
                        "ready_for_r12_safe_link_preview_rows": 0,
                    },
                }
            ),
            True,
            0,
            "",
        ),
        "weather_post_link_report_stats": (_report_stats(), True, 0, ""),
        "command_registry": ("COMMAND_REGISTRY_OK\n", True, 0, ""),
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


def _report_stats() -> str:
    return "\n".join(
        [
            "reports/phase3bb_r48/weather_feature_refresh_runtime_verification.json|1783979900|1000",
            "reports/phase3az_r12_weather/weather_missing_link_apply.json|1783979944|900",
            "reports/phase3az_r12_weather/weather_activation_preview.json|1783979964|900",
            "reports/phase3az_r12_weather/safe_to_link.csv|1783979964|1",
            "reports/phase3az_r12_weather/safe_to_relink.csv|1783979964|1",
            "reports/phase3bb_r2/weather_funnel.json|1783970000|700",
        ]
    )
