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
from kalshi_predictor.phase3bb_r41_writer_gate_normalization import (
    build_phase3bb_r41_writer_gate_normalization,
    write_phase3bb_r41_writer_gate_normalization_report,
)


def test_phase3bb_r41_unblocks_when_writer_json_valid_and_safe(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r41_writer_gate_normalization_report(
            session,
            output_dir=reports_dir / "phase3bb_r41",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["writer_gate_decision"]
    assert payload["phase"] == "3BB-R41-WRITER-GATE-NORMALIZATION"
    assert decision["status"] == "WRITER_GATE_NORMALIZED_WEATHER_FAST_LANE_UNBLOCKED"
    assert decision["weather_fast_lane_unblocked"] is True
    assert payload["parsed_writer_gate_state"]["db_writer_monitor_strict_json_valid"] is True
    assert all(row["passed"] for row in payload["writer_gate_checks"])
    assert artifacts.manifest_path.exists()


def test_phase3bb_r41_blocks_invalid_writer_json(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    bad_json = '{"safe_to_start_write": true, "recommended_next_action": "line one\nline two"}'
    runner = _fake_probe_runner(
        {
            "db_writer_monitor_raw": (bad_json, True, 0, ""),
            "db_writer_monitor_json_tool": ("", False, 1, "Invalid control character"),
        }
    )

    with session_factory() as session:
        payload = build_phase3bb_r41_writer_gate_normalization(
            session,
            output_dir=reports_dir / "phase3bb_r41",
            reports_dir=reports_dir,
            probe_runner=runner,
        )

    decision = payload["writer_gate_decision"]
    assert decision["status"] == "BLOCKED_INVALID_DB_WRITER_MONITOR_JSON"
    assert decision["weather_fast_lane_unblocked"] is False
    assert decision["first_failed_check"] == "db_writer_monitor_json_valid"


def test_phase3bb_r41_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r41-writer-gate-normalization", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r41-writer-gate-normalization" in result.output
    assert "--journal-lines" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r41.db'}")
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
    overrides: dict[str, tuple[str, bool, int | None, str]] | None = None,
):
    writer = json.dumps(
        {
            "status": "OPEN_READERS",
            "safe_to_start_write": True,
            "current_writer_pid": None,
            "writer_count": 0,
            "holder_count": 1,
            "recommended_next_action": "line one\nline two",
        },
        indent=2,
    )
    outputs = {
        "remote_time_utc": ("2026-07-13T06:00:00Z\n", True, 0, ""),
        "db_writer_monitor_raw": (writer, True, 0, ""),
        "db_writer_monitor_json_tool": ("", True, 0, ""),
        "scheduler_timer_active": ("active\n", True, 0, ""),
        "scheduler_service_active": ("inactive\n", True, 0, ""),
        "r5_service_active": ("active\n", True, 0, ""),
        "ui_service_active": ("active\n", True, 0, ""),
        "scheduler_journal": (
            "Jul 13 05:08:57 runner[1]: [phase3bb-r35] Writer active; skip writer-gated job weather_fast_lane\n",
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
