from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor import phase3bb_r10_cloud_readiness as r10
from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r10_cloud_readiness import (
    cloud_cost_plan,
    decide_cloud_readiness,
    write_phase3bb_r10_cloud_readiness_decision_report,
)


def test_phase3bb_r10_cloud_does_not_help_ev_blocker() -> None:
    decision = decide_cloud_readiness(
        {
            "writer_active": False,
            "sqlite_backend": True,
            "r5_overrunning": False,
            "r5_running": True,
            "cpu_bottleneck": False,
            "ram_bottleneck": False,
            "api_rate_limit_risk": False,
            "scheduler_needed": False,
            "categories_running": 1,
            "current_bot_blocker": "EV_NOT_POSITIVE",
        }
    )

    assert decision["status"] == "CLOUD_WOULD_NOT_HELP_CURRENT_BOTTLENECK"
    assert decision["buy_compute_now"] is False
    assert cloud_cost_plan(decision["status"])["monthly_budget_usd"] == 0


def test_phase3bb_r10_overrun_r5_needs_scheduler() -> None:
    decision = decide_cloud_readiness(
        {
            "writer_active": True,
            "sqlite_backend": True,
            "r5_overrunning": True,
            "r5_running": True,
            "cpu_bottleneck": False,
            "ram_bottleneck": False,
            "api_rate_limit_risk": False,
            "scheduler_needed": True,
            "categories_running": 2,
            "current_bot_blocker": "WATCH_NO_POSITIVE_EXPECTED_VALUE",
        }
    )

    assert decision["status"] == "NEED_ALWAYS_ON_SCHEDULER"
    assert decision["buy_compute_now"] is True
    assert cloud_cost_plan(decision["status"])["spec"].startswith("2 vCPU")


def test_phase3bb_r10_writes_requested_artifacts(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = Path(tmp_path) / "reports"
    _patch_live_probes(monkeypatch)

    with session_factory() as session:
        artifacts = write_phase3bb_r10_cloud_readiness_decision_report(
            session,
            output_dir=reports_dir / "phase3bb_r10",
            reports_dir=reports_dir,
        )

    assert artifacts.executive_summary_path.exists()
    assert artifacts.decision_markdown_path.exists()
    assert artifacts.cost_plan_path.exists()
    assert artifacts.deployment_checklist_path.exists()
    assert artifacts.decision_json_path.exists()
    assert artifacts.manifest_path.exists()
    assert "NEED_ALWAYS_ON_SCHEDULER" in artifacts.executive_summary_path.read_text(
        encoding="utf-8"
    )
    assert "2 vCPU" in artifacts.cost_plan_path.read_text(encoding="utf-8")


def test_phase3bb_r10_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r10-cloud-readiness-decision", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r10-cloud-readiness-decision" in result.output
    assert "--reports-dir" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3bb_r10.db'}")
    return get_session_factory(engine)


def _patch_live_probes(monkeypatch) -> None:
    monkeypatch.setattr(
        r10,
        "db_writer_monitor",
        lambda **_: {
            "status": "WRITER_ACTIVE",
            "safe_to_start_write": False,
            "current_writer_pid": 123,
            "current_writer_command": "kalshi-bot phase3bc-r5-unattended-start",
        },
    )
    monkeypatch.setattr(
        r10,
        "build_phase3bc_r5_status",
        lambda **_: {
            "guard": {
                "running": True,
                "status": "OVERRUNNING",
                "should_stop": True,
                "elapsed_seconds": 40000,
            },
            "latest_watch_state": "WATCHING",
            "latest_summary": {
                "phase3bc_main_blocker": "WATCH_NO_POSITIVE_EXPECTED_VALUE",
            },
        },
    )
    monkeypatch.setattr(
        r10,
        "_machine_profile",
        lambda metadata: {
            "cpu_count": 2,
            "load_average_1m_5m_15m": [0.2, 0.2, 0.2],
            "load_to_cpu_ratio_1m": 0.1,
            "cpu_bottleneck": False,
            "memory": {"available_ratio": 0.8},
            "ram_bottleneck": False,
            "workspace_disk": {},
            "database_disk": {},
        },
    )
    monkeypatch.setattr(r10, "_artifact_text_has", lambda **_: False)
