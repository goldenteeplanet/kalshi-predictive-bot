from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r14_cloud_service_plan import (
    build_phase3bb_r14_cloud_service_plan,
    write_phase3bb_r14_cloud_service_plan_report,
)


def test_phase3bb_r14_writes_draft_only_service_plan(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, recommendation="ADOPT_EXISTING_R5")

    with session_factory() as session:
        artifacts = write_phase3bb_r14_cloud_service_plan_report(
            session,
            output_dir=reports_dir / "phase3bb_r14",
            reports_dir=reports_dir,
            adopt_existing_r5=True,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    service_draft = artifacts.service_draft_path.read_text(encoding="utf-8")
    guard_draft = artifacts.guard_script_draft_path.read_text(encoding="utf-8")
    operator_command = artifacts.operator_command_path.read_text(encoding="utf-8")

    assert payload["phase"] == "3BB-R14-CLOUD-SERVICE-PLAN-DRAFT"
    assert payload["service_plan"]["status"] == "DRAFT_READY_FOR_REVIEW"
    assert payload["service_plan"]["existing_r5_pid"] == 1917
    assert payload["service_plan"]["install_allowed_now"] is False
    assert payload["service_plan"]["start_allowed_now"] is False
    assert payload["service_plan"]["enable_allowed_now"] is False
    assert payload["safety_flags"]["no_service_install"] is True
    assert payload["safety_flags"]["starts_r5_watcher"] is False
    assert payload["safety_flags"]["stops_processes"] is False
    assert "ExecStartPre=/opt/kalshi-predictive-bot/scripts/cloud/" in service_draft
    assert "kalshi-r5-start-guard.sh" in service_draft
    assert "phase3bc-r5-crypto-freshness-watch" in service_draft
    assert "Refusing duplicate R5 start" in guard_draft
    assert "systemctl" not in operator_command
    assert artifacts.manifest_path.exists()


def test_phase3bb_r14_blocks_without_adoption_gate(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, recommendation="WAIT")

    with session_factory() as session:
        payload = build_phase3bb_r14_cloud_service_plan(
            session,
            output_dir=reports_dir / "phase3bb_r14",
            reports_dir=reports_dir,
            adopt_existing_r5=True,
        )

    assert payload["service_plan"]["status"] == "BLOCKED_BY_ADOPTION_GATE"
    assert payload["service_plan"]["ready_for_review"] is False
    assert payload["service_plan"]["install_allowed_now"] is False
    assert payload["service_plan"]["start_allowed_now"] is False


def test_phase3bb_r14_blocks_without_explicit_adopt_flag(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, recommendation="ADOPT_EXISTING_R5")

    with session_factory() as session:
        payload = build_phase3bb_r14_cloud_service_plan(
            session,
            output_dir=reports_dir / "phase3bb_r14",
            reports_dir=reports_dir,
            adopt_existing_r5=False,
        )

    assert payload["service_plan"]["status"] == "BLOCKED_BY_ADOPTION_GATE"
    assert payload["service_plan"]["ready_for_review"] is False


def test_phase3bb_r14_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r14-cloud-service-plan", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r14-cloud-service-plan" in result.output
    assert "--adopt-existing-r5" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r14.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path, *, recommendation: str) -> None:
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

    r12_dir = reports_dir / "phase3bb_r12"
    r12_dir.mkdir(parents=True, exist_ok=True)
    (r12_dir / "cloud_bootstrap_verification.json").write_text(
        json.dumps(
            {
                "parsed_remote_state": {
                    "phase3ba_status": {
                        "summary": {
                            "active_writer_command": (
                                "/opt/kalshi-predictive-bot/.venv/bin/python "
                                "-m kalshi_predictor.cli "
                                "phase3bc-r5-crypto-freshness-watch "
                                "--output-dir reports/phase3bc_r5"
                            )
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    r13_dir = reports_dir / "phase3bb_r13"
    r13_dir.mkdir(parents=True, exist_ok=True)
    (r13_dir / "cloud_scheduler_adoption.json").write_text(
        json.dumps(
            {
                "cloud_target": {
                    "ssh_target": "kalshi@203.0.113.10",
                    "identity_file": "~/.ssh/id_ed25519_do",
                    "app_path": "/opt/kalshi-predictive-bot",
                    "env_path": "/etc/kalshi-bot/kalshi-bot.env",
                    "db_path": "/var/lib/kalshi-bot/kalshi_phase1.db",
                    "reports_path": "/opt/kalshi-predictive-bot/reports",
                },
                "adoption_decision": {
                    "recommendation": recommendation,
                    "current_r5_pid": 1917,
                    "guard_status": "RUNNING",
                    "guard_should_stop": False,
                    "writer_matches_r5": True,
                    "watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
                },
            }
        ),
        encoding="utf-8",
    )
