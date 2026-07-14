import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor import phase3ay
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.system_readiness import long_jobs
from kalshi_predictor.system_readiness.long_jobs import build_long_job_monitor
from kalshi_predictor.ui.app import create_app


def test_long_job_monitor_reports_phase3ay_progress_and_finish_hook(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_phase3ay_files(tmp_path, hook_pid=os.getpid())
    monkeypatch.setattr(phase3ay, "_phase3ay_running_pids", lambda: [os.getpid()])
    monkeypatch.setattr(
        long_jobs,
        "_process_runtime",
        lambda pid: {
            "pid": pid,
            "running": True,
            "command": (
                "kalshi-bot phase3ay-health-refresh --duration-hours 9 "
                "--interval-seconds 300"
            ),
            "elapsed_seconds": 3600,
        },
    )
    monkeypatch.setattr(long_jobs, "db_writer_monitor", lambda settings=None: _writer_clear())

    payload = build_long_job_monitor(settings=Settings())

    assert payload["read_only"] is True
    assert payload["safety"]["exchange_writes"] is False
    assert payload["phase3ay"]["status"] == "RUNNING"
    assert payload["phase3ay"]["duration_seconds"] == 32400
    assert payload["phase3ay"]["remaining_seconds"] == 28800
    assert payload["phase3ay"]["budget_state"] == "WITHIN_BUDGET"
    assert payload["phase3ay"]["budget_label"] == "8h 0m remaining"
    assert payload["phase3ay"]["progress_percent"] == 11.1
    assert payload["post_refresh_hook"]["status"] == "WAITING_FOR_REFRESH"
    assert "phase3bb-domain-readiness" in " ".join(
        payload["post_refresh_hook"]["planned_commands"]
    )
    assert "phase3bb-r2-general-candidate-routing" in " ".join(
        payload["post_refresh_hook"]["planned_commands"]
    )
    assert "phase3bb-r2-general-source-intake" in " ".join(
        payload["post_refresh_hook"]["planned_commands"]
    )
    assert "phase3bb-r2-general-source-evidence" in " ".join(
        payload["post_refresh_hook"]["planned_commands"]
    )
    assert "phase3bb-r2-general-source-availability" in " ".join(
        payload["post_refresh_hook"]["planned_commands"]
    )
    assert "phase3bb-r3-general-reclassification" in " ".join(
        payload["post_refresh_hook"]["planned_commands"]
    )
    assert "phase3az-gap-analysis" in " ".join(payload["post_refresh_hook"]["planned_commands"])


def test_long_job_monitor_distinguishes_budget_used_from_completion(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_phase3ay_files(tmp_path, hook_pid=os.getpid())
    monkeypatch.setattr(phase3ay, "_phase3ay_running_pids", lambda: [os.getpid()])
    monkeypatch.setattr(
        long_jobs,
        "_process_runtime",
        lambda pid: {
            "pid": pid,
            "running": True,
            "command": "kalshi-bot phase3ay-health-refresh --duration-hours 9",
            "elapsed_seconds": 33720,
        },
    )
    monkeypatch.setattr(long_jobs, "db_writer_monitor", lambda settings=None: _writer_clear())

    payload = build_long_job_monitor(settings=Settings())

    assert payload["phase3ay"]["status"] == "RUNNING"
    assert payload["phase3ay"]["budget_state"] == "OVERRUNNING"
    assert payload["phase3ay"]["remaining_label"] == "0s"
    assert payload["phase3ay"]["overrun_label"] == "22m 0s"
    assert payload["phase3ay"]["budget_label"] == "Over by 22m 0s"
    assert payload["phase3ay"]["progress_percent"] == 100.0


def test_long_job_monitor_page_and_api_render(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_phase3ay_files(tmp_path, hook_pid=os.getpid())
    monkeypatch.setattr(phase3ay, "_phase3ay_running_pids", lambda: [os.getpid()])
    monkeypatch.setattr(
        long_jobs,
        "_process_runtime",
        lambda pid: {
            "pid": pid,
            "running": True,
            "command": "kalshi-bot phase3ay-health-refresh --duration-hours 9",
            "elapsed_seconds": 7200,
        },
    )
    monkeypatch.setattr(long_jobs, "db_writer_monitor", lambda settings=None: _writer_clear())
    settings = _settings(tmp_path)
    client = TestClient(create_app(session_factory=_session_factory(tmp_path), settings=settings))

    page = client.get("/system/long-jobs")
    api = client.get("/api/long-jobs/status")
    system = client.get("/system")

    assert page.status_code == 200
    assert "Long Job Monitor" in page.text
    assert "Finish Hook" in page.text
    assert "Budget used, not completion" in page.text
    assert "Status JSON" in page.text
    assert "paper-trade" not in page.text
    assert api.status_code == 200
    assert api.json()["read_only"] is True
    assert api.json()["monitor"]["phase3ay"]["progress_percent"] == 22.2
    assert api.json()["monitor"]["phase3ay"]["budget_state"] == "WITHIN_BUDGET"
    assert system.status_code == 200
    assert "Phase 3AY Refresh Monitor" in system.text


def _seed_phase3ay_files(tmp_path: Path, *, hook_pid: int) -> None:
    output_dir = tmp_path / "reports" / "phase3ay"
    output_dir.mkdir(parents=True)
    (output_dir / "unattended_health_job.pid").write_text(str(os.getpid()), encoding="utf-8")
    (output_dir / "post_refresh_watch.pid").write_text(str(hook_pid), encoding="utf-8")
    (output_dir / "post_refresh_watch.log").write_text(
        "2026-06-27T13:55:23-05:00\nwaiting_for_phase3ay_pid_13692\n",
        encoding="utf-8",
    )
    (output_dir / "phase3ay_health_refresh.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-06-27T12:27:02+00:00",
                "status": "DEGRADED_MARKET_COVERAGE",
                "summary": {
                    "due_or_overdue": 137,
                    "eligible_exact_settlements": 2,
                    "steps_ok": 9,
                },
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "phase3ay_health_refresh_history.jsonl").write_text(
        json.dumps({"status": "DEGRADED_MARKET_COVERAGE"}) + "\n",
        encoding="utf-8",
    )


def _writer_clear() -> dict:
    return {
        "status": "OPEN_READERS",
        "safe_to_start_write": True,
        "current_writer_pid": None,
        "current_writer_command": None,
        "holder_count": 0,
        "backend": "SQLite",
        "recommended_next_action": "No writer is active.",
        "recommended_next_command_after_finish": "kalshi-bot db-locks",
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kalshi_db_url=f"sqlite:///{tmp_path / 'long_jobs.db'}",
        execution_enabled=False,
        execution_dry_run=True,
        phase_3x_professional_ux_enabled=True,
        phase_3x_mode="preview",
        phase_3t_institutional_dashboard_enabled=True,
        phase_3t_mode="read_only_shadow",
    )


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{tmp_path / 'long_jobs.db'}")
    return get_session_factory(engine)
