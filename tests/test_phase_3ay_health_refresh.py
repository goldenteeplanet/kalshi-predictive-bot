import json
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from kalshi_predictor import phase3ay
from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3ay import (
    start_phase3ay_unattended_refresh,
    write_phase3ay_health_refresh_report,
    write_phase3ay_status_report,
    write_phase3ay_unattended_guard_report,
)
from kalshi_predictor.scheduler import scheduler_plan
from kalshi_predictor.utils.time import utc_now


def test_phase3ay_health_refresh_writes_fresh_watch_report(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ay, "db_writer_monitor", lambda settings=None: _clear_writer())
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        artifacts = write_phase3ay_health_refresh_report(
            session,
            output_dir=Path("reports/phase3ay"),
            settings=Settings(),
            interval_seconds=300,
            step_jobs=_fake_step_jobs(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["status"] == "FRESH_WATCHING_SETTLEMENTS"
    assert payload["summary"]["due_or_overdue"] == 2
    assert payload["summary"]["market_snapshots_inserted"] == 8
    assert payload["summary"]["sports_placeholder_gate"] == "HOLD_PLACEHOLDER_UPGRADES"
    assert payload["safety"]["live_or_demo_execution"] is False
    assert payload["safety"]["exact_ticker_settlement_required"] is True
    assert "phase3ay-health-refresh" in artifacts.markdown_path.read_text(encoding="utf-8")


def test_phase3ay_health_refresh_waits_for_existing_writer(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        phase3ay,
        "db_writer_monitor",
        lambda settings=None: {
            **_clear_writer(),
            "status": "WRITER_ACTIVE",
            "current_writer_pid": 123,
            "current_writer_command": "kalshi-bot build-sports-features",
        },
    )
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        artifacts = write_phase3ay_health_refresh_report(
            session,
            output_dir=Path("reports/phase3ay"),
            settings=Settings(),
            step_jobs=_fake_step_jobs(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["status"] == "WAITING_FOR_DB_WRITER"
    assert payload["steps"] == []
    assert payload["summary"]["paper_status"] == "STALE"


def test_phase3ay_market_collect_clean_timeout_writes_resume_checkpoint(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ay, "db_writer_monitor", lambda settings=None: _clear_writer())
    session_factory = _session_factory(tmp_path)

    def fake_collect_once(**kwargs):
        callback = kwargs["page_callback"]
        callback(
            "market_sync",
            {
                "event": "page",
                "pages_seen": 1,
                "request_cursor": None,
                "resume_cursor": "CURSOR_2",
                "next_cursor": "CURSOR_2",
                "markets_on_page": 100,
                "has_more": True,
            },
        )
        callback(
            "market_sync",
            {
                "event": "stop",
                "stop_reason": "deadline",
                "pages_seen": 1,
                "resume_cursor": "CURSOR_2",
            },
        )
        return {
            "markets_seen": 100,
            "snapshots_inserted": 0,
            "forecasts_inserted": 0,
            "skipped_forecasts": 0,
            "collection_status": "TIMED_OUT_CLEANLY",
            "stopped_reason": "deadline",
            "resume_cursor": "CURSOR_2",
            "market_pages_processed": 1,
            "snapshot_pages_processed": 0,
        }

    monkeypatch.setattr(phase3ay, "collect_once", fake_collect_once)
    step_jobs = _fake_step_jobs()
    step_jobs.pop("market_collect")

    with session_factory() as session:
        artifacts = write_phase3ay_health_refresh_report(
            session,
            output_dir=Path("reports/phase3ay"),
            settings=Settings(),
            step_jobs=step_jobs,
            duration_budget_seconds=1.0,
            deadline_monotonic=time.monotonic() + 30,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    checkpoint = json.loads(
        Path("reports/phase3ay/phase3ay_market_checkpoint.json").read_text(encoding="utf-8")
    )

    assert payload["status"] == "TIMED_OUT_CLEANLY"
    assert payload["refresh_guard"]["market_checkpoint"]["status"] == "TIMED_OUT_CLEANLY"
    assert checkpoint["resume_cursor"] == "CURSOR_2"
    assert checkpoint["safe_to_resume"] is True
    assert "all-markets" in checkpoint["resume_command"]
    assert payload["safety"]["live_or_demo_execution"] is False


def test_phase3ay_market_collect_resumes_from_prior_checkpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ay, "db_writer_monitor", lambda settings=None: _clear_writer())
    output_dir = Path("reports/phase3ay")
    output_dir.mkdir(parents=True)
    (output_dir / "phase3ay_market_checkpoint.json").write_text(
        json.dumps(
            {
                "status": "PARTIAL_REFRESH_CONTINUABLE",
                "resume_cursor": "CURSOR_2",
                "safe_to_resume": True,
            }
        ),
        encoding="utf-8",
    )
    session_factory = _session_factory(tmp_path)
    observed: dict[str, str | None] = {}

    def fake_collect_once(**kwargs):
        observed["start_cursor"] = kwargs.get("start_cursor")
        return {
            "markets_seen": 20,
            "snapshots_inserted": 20,
            "forecasts_inserted": 20,
            "skipped_forecasts": 0,
            "collection_status": "COMPLETE",
            "stopped_reason": None,
            "resume_cursor": None,
            "market_pages_processed": 1,
            "snapshot_pages_processed": 1,
        }

    monkeypatch.setattr(phase3ay, "collect_once", fake_collect_once)
    step_jobs = _fake_step_jobs()
    step_jobs.pop("market_collect")

    with session_factory() as session:
        artifacts = write_phase3ay_health_refresh_report(
            session,
            output_dir=output_dir,
            settings=Settings(),
            step_jobs=step_jobs,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    checkpoint = json.loads((output_dir / "phase3ay_market_checkpoint.json").read_text())

    assert observed["start_cursor"] == "CURSOR_2"
    assert checkpoint["status"] == "COMPLETE"
    assert checkpoint["resume_cursor"] is None
    assert checkpoint["safe_to_resume"] is False
    assert payload["market_health"]["collection_status"] == "COMPLETE"


def test_phase3ay_status_report_is_read_only(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ay, "_phase3ay_running_pids", lambda: [999])
    output_dir = Path("reports/phase3ay")
    output_dir.mkdir(parents=True)
    (output_dir / "unattended_health_job.pid").write_text("999", encoding="utf-8")
    (output_dir / "phase3ay_health_refresh.json").write_text(
        json.dumps({"generated_at": "now", "status": "FRESH", "summary": {"steps_ok": 9}}),
        encoding="utf-8",
    )

    artifacts = write_phase3ay_status_report(output_dir=output_dir)
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["process"]["status"] == "RUNNING"
    assert payload["guard"]["status"] == "RUNNING_UNKNOWN_BUDGET"
    assert payload["latest_summary"]["steps_ok"] == 9


def test_phase3ay_unattended_start_writes_pid_logs_and_metadata(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ay, "_phase3ay_running_pids", lambda: [])
    observed: dict[str, object] = {}

    class FakePopen:
        pid = 4242

    def fake_popen(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return FakePopen()

    monkeypatch.setattr(phase3ay.subprocess, "Popen", fake_popen)

    result = start_phase3ay_unattended_refresh(
        output_dir=Path("reports/phase3ay"),
        cycles=2,
        interval_seconds=60,
        duration_hours=0.25,
        all_markets=False,
        market_collect=False,
        market_max_pages=1,
        include_orderbook=False,
        settlement_max_pages=1,
        settlement_commit_every=1000,
        settlement_only=True,
        timeout_grace_seconds=30,
    )

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))

    assert result.started is True
    assert result.pid == 4242
    assert result.pid_path.read_text(encoding="utf-8") == "4242"
    assert "phase3ay-health-refresh" in metadata["command"]
    assert metadata["timeout_seconds"] == 930
    assert metadata["paper_only_safety"] == "PAPER_ONLY_NO_EXCHANGE_WRITES"
    assert metadata["settlement_only"] is True
    assert metadata["market_collect"] is False
    assert metadata["include_orderbook"] is False
    assert metadata["settlement_commit_every"] == 1000
    assert "--paged-markets" in observed["command"]
    assert "--no-market-collect" in observed["command"]
    assert "--no-orderbook" in observed["command"]
    assert "--settlement-commit-every" in observed["command"]
    assert "1000" in observed["command"]
    assert "--settlement-only" in observed["command"]


def test_phase3ay_settlement_only_skips_slow_non_settlement_steps(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ay, "db_writer_monitor", lambda settings=None: _clear_writer())

    with _session_factory(tmp_path)() as session:
        artifacts = write_phase3ay_health_refresh_report(
            session,
            output_dir=Path("reports/phase3ay"),
            step_jobs=_fake_step_jobs(),
            settlement_only=True,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    steps = {step["name"]: step for step in payload["steps"]}

    assert payload["mode"] == "PAPER_SETTLEMENT_ONLY_REFRESH_LOOP"
    assert payload["summary"]["settlement_only"] is True
    assert steps["settlement_sync"]["status"] == "OK"
    assert steps["exact_settlement_harvest"]["status"] == "OK"
    assert steps["paper_realize"]["status"] == "OK"
    assert steps["paper_settlement_doctor"]["status"] == "OK"
    assert steps["market_coverage_doctor"]["status"] == "SKIPPED"
    assert steps["active_universe_doctor"]["status"] == "SKIPPED"
    assert steps["sports_placeholder_resolution"]["status"] == "SKIPPED"
    assert steps["sports_placeholder_watch"]["status"] == "SKIPPED"
    assert steps["phase_orchestrator"]["status"] == "SKIPPED"
    assert payload["status"] in {"FRESH_SETTLEMENT_ONLY", "FRESH_WATCHING_SETTLEMENTS"}


def test_phase3ay_status_marks_unattended_overrun(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ay, "_phase3ay_running_pids", lambda: [4242])
    output_dir = Path("reports/phase3ay")
    output_dir.mkdir(parents=True)
    (output_dir / "unattended_health_job.pid").write_text("4242", encoding="utf-8")
    (output_dir / "unattended_health_job.json").write_text(
        json.dumps(
            {
                "started_at": (utc_now() - timedelta(seconds=120)).isoformat(),
                "pid": 4242,
                "timeout_seconds": 30,
                "duration_budget_seconds": 10,
                "stdout_path": "reports/phase3ay/unattended_stdout.log",
                "stderr_path": "reports/phase3ay/unattended_stderr.log",
            }
        ),
        encoding="utf-8",
    )

    artifacts = write_phase3ay_status_report(output_dir=output_dir)
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert payload["guard"]["status"] == "OVERRUNNING"
    assert payload["guard"]["should_stop"] is True
    assert "phase3ay-unattended-guard --stop-overrun" in payload["recommended_next_action"]


def test_phase3ay_unattended_guard_can_stop_overrun(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phase3ay, "_phase3ay_running_pids", lambda: [4242])
    stopped: dict[str, int] = {}

    def fake_terminate(pid, *, grace_seconds):
        stopped["pid"] = pid
        stopped["grace_seconds"] = grace_seconds
        return {"status": "STOPPED_AFTER_TERM", "pid": pid}

    monkeypatch.setattr(phase3ay, "_terminate_pid", fake_terminate)
    output_dir = Path("reports/phase3ay")
    output_dir.mkdir(parents=True)
    (output_dir / "unattended_health_job.pid").write_text("4242", encoding="utf-8")
    (output_dir / "unattended_health_job.json").write_text(
        json.dumps(
            {
                "started_at": (utc_now() - timedelta(seconds=120)).isoformat(),
                "pid": 4242,
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )

    artifacts = write_phase3ay_unattended_guard_report(
        output_dir=output_dir,
        stop_overrun=True,
        terminate_grace_seconds=3,
    )
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    assert stopped == {"pid": 4242, "grace_seconds": 3}
    assert payload["action"]["termination_result"]["status"] == "STOPPED_AFTER_TERM"


def test_phase3ay_cli_and_scheduler_smoke() -> None:
    result = CliRunner().invoke(app, ["phase3ay-health-refresh", "--help"])

    assert result.exit_code == 0
    assert "phase3ay-health-refresh" in result.output
    assert "Commit broad" in result.output
    assert "--settlement-only" in result.output
    status = CliRunner().invoke(app, ["phase3ay-status", "--help"])
    assert status.exit_code == 0
    assert "phase3ay-status" in status.output
    start = CliRunner().invoke(app, ["phase3ay-unattended-start", "--help"])
    assert start.exit_code == 0
    assert "phase3ay-unattended-start" in start.output
    assert "--no-market-collect" in start.output
    assert "--no-orderbook" in start.output
    assert "Commit broad" in start.output
    assert "--settlement-only" in start.output
    guard = CliRunner().invoke(app, ["phase3ay-unattended-guard", "--help"])
    assert guard.exit_code == 0
    assert "phase3ay-unattended-guard" in guard.output
    plan = scheduler_plan("paper-market-health-watch")
    assert plan[0].command.startswith("kalshi-bot phase3ay-unattended-start")
    assert plan[0].every_minutes == 5


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ay.db'}")
    return get_session_factory(engine)


def _clear_writer() -> dict:
    return {
        "status": "CLEAR",
        "diagnostics_status": "CLEAR",
        "safe_to_start_write": True,
        "current_writer": None,
        "current_writer_pid": None,
        "current_writer_command": None,
    }


def _fake_step_jobs() -> dict:
    return {
        "market_collect": lambda: {
            "markets_seen": 10,
            "snapshots_inserted": 8,
            "forecasts_inserted": 8,
            "skipped_forecasts": 0,
        },
        "settlement_sync": lambda: 0,
        "exact_settlement_harvest": lambda: _artifact(
            Path("reports/phase3aa_r2/phase3aa_r2_exact_settlement_harvest.json"),
            {
                "summary": {
                    "exact_tickers_checked": 2,
                    "exact_settlements_written": 0,
                    "eligible_exact_settlements_after": 0,
                    "fetch_errors": 0,
                    "source_settled_without_usable_outcome": 2,
                }
            },
        ),
        "paper_realize": lambda: _artifact(
            Path("reports/phase3aa/phase3aa_outcome_realizer.json"),
            {
                "pnl_realized": False,
                "eta_schedule": {
                    "summary": {
                        "active_unsettled": 3,
                        "due_or_overdue": 2,
                        "eligible_exact_settlements": 0,
                    }
                },
            },
        ),
        "paper_settlement_doctor": lambda: _artifact(
            Path("reports/paper_settlement_reconciliation/paper_settlement_reconciliation.json"),
            {"summary": {"eligible_to_settle_now": 0, "still_open_or_unsettled": 2}},
        ),
        "market_coverage_doctor": lambda: _artifact(
            Path("reports/market_coverage/market_coverage_doctor.json"),
            {
                "coverage_rows": [{"scope": "sports", "health": "HEALTHY"}],
                "recommendations": [],
            },
        ),
        "active_universe_doctor": lambda: _artifact(
            Path("reports/phase3as/phase3as_active_universe.json"),
            {"summary": {"ready_universe_for_forecasts": 4}},
        ),
        "sports_placeholder_resolution": lambda: _artifact(
            Path("reports/phase3ah_sports/phase3ah_round_placeholder_resolution_report.json"),
            {"summary": {"still_placeholder_rows": 8, "safe_to_apply_rows": 0}},
        ),
        "sports_placeholder_watch": lambda: _artifact(
            Path("reports/phase3ah_sports/phase3ah_sports_placeholder_watch.json"),
            {
                "summary": {
                    "phase3ae_gate_status": "HOLD_PLACEHOLDER_UPGRADES",
                    "placeholder_rows_reviewed": 8,
                    "safe_to_apply_rows": 0,
                    "still_placeholder_rows": 8,
                }
            },
        ),
        "phase_orchestrator": lambda: _artifact(
            Path("reports/phase_orchestrator.json"),
            {"summary": {"next_phase": "3AH"}},
        ),
    }


def _artifact(path: Path, payload: dict) -> SimpleNamespace:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    markdown_path = path.with_suffix(".md")
    markdown_path.write_text("ok", encoding="utf-8")
    return SimpleNamespace(json_path=path, markdown_path=markdown_path)
