from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_status
from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.utils.time import utc_now


def _running_r5_status() -> dict[str, object]:
    return {
        "process": {"status": "RUNNING"},
        "guard": {"status": "RUNNING", "should_stop": False},
        "latest_watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
        "latest_summary": {"watch_state": "WAITING_FOR_EXECUTABLE_BOOK"},
    }


def test_phase3ba_status_waits_on_active_non_overrun_writer() -> None:
    next_action = phase3ba_status._choose_next_action(
        writer={
            "current_writer_pid": 123,
            "current_writer_command": "kalshi-bot snapshot --status open",
        },
        r5_status=_running_r5_status(),
        truth={},
        category_backlog={},
        summary={
            "r5_should_stop": False,
            "paper_ready_rows": 0,
            "weather_first_blocker": "SNAPSHOT_MISSING",
        },
    )

    assert next_action["stage"] == "WAIT_FOR_ACTIVE_WRITER"
    assert next_action["clearly_wait"] is True
    assert next_action["command"] == "kalshi-bot db-writer-monitor --json"


def test_phase3ba_status_does_not_recommend_duplicate_r5_start() -> None:
    next_action = phase3ba_status._choose_next_action(
        writer={"current_writer_pid": None},
        r5_status=_running_r5_status(),
        truth={},
        category_backlog={},
        summary={
            "r5_should_stop": False,
            "paper_ready_rows": 0,
            "weather_first_blocker": "SNAPSHOT_MISSING",
        },
    )
    checks = phase3ba_status._command_checks(next_action["command"])

    assert checks["r5_start_commands"] == []
    assert "phase3bc-r5-unattended-start" not in next_action["command"]
    assert "phase3ax-r9-guarded-refresh-job" not in next_action["command"]


def test_phase3ba_status_command_checks_registered_and_safe() -> None:
    command = (
        "kalshi-bot db-writer-monitor --json\n"
        "kalshi-bot snapshot --status open --limit 100 --max-pages 3 "
        "--series-ticker KXTEMPNYCH --include-orderbook\n"
        "kalshi-bot phase3ba-r2-weather-ranking-activation --output-dir "
        "reports/phase3ba_r2 --reports-dir reports --limit 100"
    )

    checks = phase3ba_status._command_checks(command)

    assert checks["all_recommended_commands_registered"] is True
    assert checks["contains_forbidden_trade_command"] is False


def test_phase3ba_status_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-status", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-status" in result.output


def test_phase3ba_status_uses_r5_status_json_positive_ev_truth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reports_dir = tmp_path / "reports"
    _write_r5_status_json(reports_dir)
    session_factory = _session_factory(tmp_path)
    monkeypatch.setattr(
        phase3ba_status,
        "db_writer_monitor",
        lambda settings=None: {
            "safe_to_start_write": True,
            "current_writer_pid": None,
            "current_writer_command": None,
        },
    )

    with session_factory() as session:
        payload = phase3ba_status.build_phase3ba_status(
            session,
            output_dir=reports_dir / "phase3ba_status",
            reports_dir=reports_dir,
        )

    summary = payload["summary"]
    assert summary["r5_truth_source"].endswith(
        "reports/phase3bc_r5/phase3bc_r5_status.json"
    )
    assert summary["r5_running"] is True
    assert summary["r5_guard_status"] == "RUNNING"
    assert summary["r5_watch_state"] == "WAITING_FOR_EXECUTABLE_BOOK"
    assert summary["crypto_positive_ev_rows"] == 4
    assert summary["crypto_paper_ready_rows"] == 0
    assert summary["crypto_positive_ev_no_executable_book_rows"] == 4
    assert summary["crypto_evidence_scope"] == "R5_AGGREGATE_TRUTH_ONLY"
    assert summary["crypto_first_blocker"] == "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    assert summary["crypto_first_blocker"] != "NO_CURRENT_ROWS"
    assert summary["paper_ready_rows"] == 0
    assert summary["positive_ev_rows"] == 4
    assert summary["true_first_blocker"] == "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    assert summary["true_first_blocker"] != "PAPER_READY"


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3ba_status.db'}")
    return get_session_factory(engine)


def _write_r5_status_json(reports_dir: Path) -> None:
    status_dir = reports_dir / "phase3bc_r5"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "phase3bc_r5_status.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "process": {"status": "RUNNING"},
                "guard": {
                    "status": "RUNNING",
                    "should_stop": False,
                    "positive_ev_rows": 4,
                    "paper_ready_candidates": 0,
                },
                "latest_watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
                "latest_summary": {
                    "watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
                    "positive_ev_rows": 4,
                    "paper_ready_candidates": 0,
                    "positive_ev_no_executable_book_rows": 4,
                    "primary_gap_after_refresh": "LOW_EDGE_OR_SCORE_BLOCK",
                    "live_or_demo_execution": False,
                    "order_submission": False,
                    "order_cancel_replace": False,
                },
            }
        ),
        encoding="utf-8",
    )
