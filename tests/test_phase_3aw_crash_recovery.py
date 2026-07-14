import json
import os
from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3au import LongJobHeartbeat
from kalshi_predictor.phase3aw import (
    CRASHED_OR_INTERRUPTED,
    EV_NOT_POSITIVE,
    LEGACY_WRITER_ACTIVE,
    RUNNING,
    STOPPED_EARLY,
    build_phase3aw_recovery_status,
    write_phase3aw_dashboard_truth_report,
    write_phase3aw_recovery_report,
)
from kalshi_predictor.ui.service import _paper_trade_blocker_status_from_phase3aw
from kalshi_predictor.utils.time import utc_now


def _monitor_payload(
    *,
    pid: int | None = None,
    safe_to_start_write: bool = True,
    command: str | None = None,
) -> dict[str, object]:
    return {
        "current_writer_pid": pid,
        "current_writer_command": command,
        "current_writer_elapsed": "1m 00s" if pid else None,
        "safe_to_start_write": safe_to_start_write,
    }


def test_phase3aw_classifies_active_writer_with_fresh_heartbeat(tmp_path) -> None:
    heartbeat = LongJobHeartbeat("link-remediate", output_dir=Path(tmp_path))
    heartbeat.emit(
        stage="SPORTS_LINK_START",
        processed=12,
        total=100,
        current_item="KXTEST",
        message="running",
        force_checkpoint=True,
    )

    status = build_phase3aw_recovery_status(
        heartbeat_dir=Path(tmp_path),
        stale_after_seconds=99999,
        monitor_payload=_monitor_payload(
            pid=os.getpid(),
            safe_to_start_write=False,
            command="kalshi-bot link-remediate --resume",
        ),
    )

    assert status["classification"] == RUNNING
    assert status["safe_to_resume"] is False
    assert status["last_stage"] == "SPORTS_LINK_START"
    assert "Wait" in status["recommended_next_action"]


def test_phase3aw_classifies_stale_heartbeat_without_writer_as_crashed(tmp_path) -> None:
    old_time = utc_now() - timedelta(minutes=20)
    payload = {
        "job_name": "link-remediate",
        "pid": 12345,
        "heartbeat_at": old_time.isoformat(),
        "stage": "SPORTS_LINK_START",
        "processed": 50,
        "total": 100,
        "current_item": "KXSTALE",
        "message": "old heartbeat",
    }
    heartbeat_path = Path(tmp_path) / "link_remediate_heartbeat.json"
    checkpoint_path = Path(tmp_path) / "link_remediate_checkpoint.json"
    heartbeat_path.write_text(json.dumps(payload), encoding="utf-8")
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")

    status = build_phase3aw_recovery_status(
        heartbeat_dir=Path(tmp_path),
        stale_after_seconds=60,
        monitor_payload=_monitor_payload(safe_to_start_write=True),
    )

    assert status["classification"] == CRASHED_OR_INTERRUPTED
    assert status["safe_to_resume"] is True
    assert "link-remediate --resume" in status["resume_command"]


def test_phase3aw_classifies_writer_without_heartbeat_as_legacy_active(tmp_path) -> None:
    status = build_phase3aw_recovery_status(
        heartbeat_dir=Path(tmp_path),
        stale_after_seconds=60,
        monitor_payload=_monitor_payload(
            pid=222,
            safe_to_start_write=False,
            command="kalshi-bot old-writer",
        ),
    )

    assert status["classification"] == LEGACY_WRITER_ACTIVE
    assert status["safe_to_resume"] is False
    assert "Do not start another writer" in status["recommended_next_action"]


def test_phase3aw_stopped_early_can_resume_when_no_writer(tmp_path) -> None:
    LongJobHeartbeat("link-remediate", output_dir=Path(tmp_path)).emit(
        stage="STOPPED_EARLY",
        processed=20,
        total=100,
        current_item="KXSTOP",
        message="time budget reached",
        force_checkpoint=True,
    )

    status = build_phase3aw_recovery_status(
        heartbeat_dir=Path(tmp_path),
        stale_after_seconds=99999,
        monitor_payload=_monitor_payload(safe_to_start_write=True),
    )

    assert status["classification"] == STOPPED_EARLY
    assert status["safe_to_resume"] is True
    assert "link-remediate --resume" in status["recommended_next_action"]


def test_phase3aw_report_renders_markdown_and_json(tmp_path) -> None:
    LongJobHeartbeat("link-remediate", output_dir=Path(tmp_path) / "heartbeat").emit(
        stage="STOPPED_EARLY",
        processed=20,
        total=100,
        current_item="KXSTOP",
        message="time budget reached",
        force_checkpoint=True,
    )

    artifacts = write_phase3aw_recovery_report(
        output_dir=Path(tmp_path) / "report",
        heartbeat_dir=Path(tmp_path) / "heartbeat",
        stale_after_seconds=99999,
        monitor_payload=_monitor_payload(safe_to_start_write=True),
    )

    markdown = artifacts["markdown_path"].read_text(encoding="utf-8")
    payload = json.loads(artifacts["json_path"].read_text(encoding="utf-8"))
    assert "Phase 3AW Long Job Crash Recovery Report" in markdown
    assert "STOPPED_EARLY" in markdown
    assert payload["classification"] == STOPPED_EARLY


def test_phase3aw_dashboard_truth_ignores_conflicting_phase3ar_positive_ev(
    tmp_path,
) -> None:
    reports_dir = Path(tmp_path) / "reports"
    output_dir = reports_dir / "phase3aw"
    _write_r5_ev_not_positive_status(reports_dir)
    _write_conflicting_phase3ar_gate(reports_dir)
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        artifacts = write_phase3aw_dashboard_truth_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
            command_args=[
                "phase3aw-dashboard-truth",
                "--output-dir",
                str(output_dir),
                "--reports-dir",
                str(reports_dir),
            ],
        )

    payload = json.loads(artifacts.dashboard_truth_path.read_text(encoding="utf-8"))
    audit = json.loads(artifacts.stale_artifact_audit_path.read_text(encoding="utf-8"))
    panel = _paper_trade_blocker_status_from_phase3aw(payload)

    assert payload["summary"]["true_current_blocker"] == EV_NOT_POSITIVE
    assert payload["summary"]["current_positive_ev_rows"] == 0
    assert payload["summary"]["paper_ready_candidates"] == 0
    assert payload["summary"]["stale_artifacts_ignored"] >= 1
    assert any(
        row["name"] == "Phase 3AR paper-ready gate"
        and row["classification"] == "CONFLICTS_WITH_R5"
        for row in audit["rows"]
    )
    assert panel["status_label"] == "Waiting for Positive EV"
    assert {item["label"]: item["value"] for item in panel["metrics"]}[
        "Positive EV"
    ] == 0
    assert panel["positive_ev_rows"] == []


def test_phase3aw_dashboard_labels_active_snapshot_catchup_without_stale_badge(
    tmp_path,
) -> None:
    reports_dir = Path(tmp_path) / "reports"
    output_dir = reports_dir / "phase3aw"
    _write_r5_snapshot_refreshing_status(reports_dir)
    _write_conflicting_phase3ar_gate(reports_dir)
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        artifacts = write_phase3aw_dashboard_truth_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
        )

    payload = json.loads(artifacts.dashboard_truth_path.read_text(encoding="utf-8"))
    panel = _paper_trade_blocker_status_from_phase3aw(payload)

    assert payload["summary"]["true_current_blocker"] == "SNAPSHOT_STALE"
    assert payload["summary"]["true_current_blocker_label"] == "Refreshing snapshots"
    assert panel["status_label"] == "Refreshing snapshots"
    assert panel["summary"] == (
        "Current crypto evidence is still catching up, so paper readiness is blocked."
    )
    display_text = " ".join(
        [
            panel["status_label"],
            panel["summary"],
            *[str(metric.get("label")) for metric in panel["metrics"]],
            *[
                str(blocker.get("area"))
                + " "
                + str(blocker.get("source"))
                + " "
                + str(blocker.get("status_label"))
                + " "
                + str(blocker.get("evidence"))
                + " "
                + str(blocker.get("next_action"))
                for blocker in panel["blockers"]
            ],
        ]
    ).lower()
    assert "stale" not in display_text


def test_phase3aw_dashboard_uses_ev_gap_after_classified_freshness_backlog(
    tmp_path,
) -> None:
    reports_dir = Path(tmp_path) / "reports"
    output_dir = reports_dir / "phase3aw"
    _write_r5_snapshot_refreshing_status(reports_dir)
    status_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json"
    watch_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_crypto_freshness_watch.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    watch = json.loads(watch_path.read_text(encoding="utf-8"))
    for summary in (status["latest_summary"], watch["summary"]):
        summary["data_freshness_gap_after_refresh"] = "SNAPSHOT_STALE"
        summary["primary_gap_after_refresh"] = EV_NOT_POSITIVE
        summary["snapshot_backlog_status"] = "EXACT_TICKER_NOT_REFRESHED"
        summary["forecast_backlog_status"] = (
            "FORECAST_REFRESH_PENDING_AFTER_SNAPSHOT_REFRESH"
        )
        summary["freshness_backlog_blocks_current_positive_ev"] = False
        summary["positive_ev_snapshot_stale_rows"] = 0
        summary["positive_ev_forecast_stale_rows"] = 0
    status_path.write_text(json.dumps(status), encoding="utf-8")
    watch_path.write_text(json.dumps(watch), encoding="utf-8")
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        artifacts = write_phase3aw_dashboard_truth_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
        )

    payload = json.loads(artifacts.dashboard_truth_path.read_text(encoding="utf-8"))
    panel = _paper_trade_blocker_status_from_phase3aw(payload)

    assert payload["summary"]["true_current_blocker"] == EV_NOT_POSITIVE
    assert payload["current_crypto_funnel"]["data_freshness_gap_after_refresh"] == (
        "SNAPSHOT_STALE"
    )
    assert payload["current_crypto_funnel"]["snapshot_backlog_status"] == (
        "EXACT_TICKER_NOT_REFRESHED"
    )
    assert panel["status_label"] == "Waiting for Positive EV"


def test_phase3aw_dashboard_keeps_running_overdue_r5_as_runner_state(
    tmp_path,
) -> None:
    reports_dir = Path(tmp_path) / "reports"
    output_dir = reports_dir / "phase3aw"
    _write_r5_snapshot_refreshing_status(reports_dir)
    status_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json"
    watch_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_crypto_freshness_watch.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    watch = json.loads(watch_path.read_text(encoding="utf-8"))
    status["guard"]["stale_report"] = True
    status["guard"]["latest_age_seconds"] = 900
    status["guard"]["freshness_window_minutes"] = 10
    for summary in (status["latest_summary"], watch["summary"]):
        summary["data_freshness_gap_after_refresh"] = "SNAPSHOT_STALE"
        summary["primary_gap_after_refresh"] = EV_NOT_POSITIVE
        summary["freshness_backlog_blocks_current_positive_ev"] = False
        summary["positive_ev_snapshot_stale_rows"] = 0
        summary["positive_ev_forecast_stale_rows"] = 0
    status_path.write_text(json.dumps(status), encoding="utf-8")
    watch_path.write_text(json.dumps(watch), encoding="utf-8")
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        artifacts = write_phase3aw_dashboard_truth_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
        )

    payload = json.loads(artifacts.dashboard_truth_path.read_text(encoding="utf-8"))
    panel = _paper_trade_blocker_status_from_phase3aw(payload)
    watcher_row = next(
        row for row in panel["blockers"] if row["area"] == "Watcher freshness"
    )

    assert payload["summary"]["true_current_blocker"] == EV_NOT_POSITIVE
    assert payload["summary"]["r5_runner_state"] == "RUNNING_CYCLE_OVERDUE"
    assert payload["current_crypto_funnel"]["r5_stale_report"] is True
    assert panel["status_label"] == "Waiting for Positive EV"
    assert watcher_row["status_label"] == "Refresh running / cycle overdue"
    assert "phase3ax-r9-guarded-refresh-job" in payload["operator_next_command"]
    assert "--status-only" in payload["operator_next_command"]


def test_phase3aw_dashboard_prefers_low_edge_over_unrelated_snapshot_backlog(
    tmp_path,
) -> None:
    reports_dir = Path(tmp_path) / "reports"
    output_dir = reports_dir / "phase3aw"
    _write_r5_snapshot_refreshing_status(reports_dir)
    status_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json"
    watch_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_crypto_freshness_watch.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    watch = json.loads(watch_path.read_text(encoding="utf-8"))
    for summary in (status["latest_summary"], watch["summary"]):
        summary["positive_ev_rows"] = 1
        summary["primary_gap_after_refresh"] = "LOW_EDGE_OR_SCORE_BLOCK"
        summary["data_freshness_gap_after_refresh"] = "SNAPSHOT_STALE"
        summary["snapshot_backlog_status"] = "EXACT_TICKER_NOT_REFRESHED"
        summary["freshness_backlog_blocks_current_positive_ev"] = False
        summary["positive_ev_snapshot_stale_rows"] = 0
        summary["positive_ev_forecast_stale_rows"] = 0
    status_path.write_text(json.dumps(status), encoding="utf-8")
    watch_path.write_text(json.dumps(watch), encoding="utf-8")
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        artifacts = write_phase3aw_dashboard_truth_report(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=Settings(opportunity_min_time_to_close_minutes=1),
        )

    payload = json.loads(artifacts.dashboard_truth_path.read_text(encoding="utf-8"))
    panel = _paper_trade_blocker_status_from_phase3aw(payload)

    assert payload["summary"]["true_current_blocker"] == "LOW_EDGE_OR_SCORE_BLOCK"
    assert payload["current_crypto_funnel"]["snapshot_backlog_status"] == (
        "EXACT_TICKER_NOT_REFRESHED"
    )
    assert panel["status_label"] == "Edge or score below threshold"


def test_phase3aw_cli_help() -> None:
    runner = CliRunner()

    for command in ("phase3aw-status", "phase3aw-crash-report", "phase3aw-dashboard-truth"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3aw.db'}")
    return get_session_factory(engine)


def _write_r5_ev_not_positive_status(reports_dir: Path) -> None:
    r5_dir = reports_dir / "phase3bc_r5"
    r5_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now().isoformat()
    summary = {
        "watch_state": "WAITING_FOR_POSITIVE_EV",
        "active_pure_crypto_rows": 27,
        "current_active_window_rows": 27,
        "snapshot_stale_rows": 0,
        "snapshot_missing_rows": 0,
        "forecast_stale_rows": 0,
        "forecast_missing_rows": 0,
        "ranking_missing_rows": 0,
        "ranking_stale_rows": 0,
        "ranking_before_forecast_rows": 0,
        "true_ranking_gap_after_repair": 0,
        "ranking_coverage_gap_after_repair": 0,
        "primary_gap_after_refresh": "EV_NOT_POSITIVE",
        "phase3bc_main_blocker": "WATCH_NO_POSITIVE_EXPECTED_VALUE",
        "positive_ev_rows": 0,
        "clean_execution_rows": 14,
        "paper_ready_candidates": 0,
        "best_ev_candidate_ticker": "KXBTC-TEST",
        "best_current_expected_value_cents": "-1.0",
        "best_ev_gap_to_positive_cents": "1.0",
    }
    status = {
        "generated_at": now,
        "latest_report_generated_at": now,
        "guard": {
            "status": "RUNNING",
            "running": True,
            "stale_report": False,
        },
        "latest_summary": summary,
    }
    watch = {
        "generated_at": now,
        "summary": summary,
    }
    (r5_dir / "phase3bc_r5_status.json").write_text(
        json.dumps(status),
        encoding="utf-8",
    )
    (r5_dir / "phase3bc_r5_crypto_freshness_watch.json").write_text(
        json.dumps(watch),
        encoding="utf-8",
    )


def _write_r5_snapshot_refreshing_status(reports_dir: Path) -> None:
    r5_dir = reports_dir / "phase3bc_r5"
    r5_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now().isoformat()
    summary = {
        "watch_state": "REFRESH_SNAPSHOTS",
        "active_pure_crypto_rows": 1000,
        "current_active_window_rows": 212,
        "snapshot_stale_rows": 88,
        "snapshot_missing_rows": 0,
        "forecast_stale_rows": 43,
        "forecast_missing_rows": 0,
        "ranking_missing_rows": 0,
        "ranking_stale_rows": 0,
        "ranking_before_forecast_rows": 0,
        "true_ranking_gap_after_repair": 0,
        "ranking_coverage_gap_after_repair": 0,
        "primary_gap_after_refresh": "SNAPSHOT_STALE",
        "phase3bc_main_blocker": "WATCH_NO_POSITIVE_EXPECTED_VALUE",
        "positive_ev_rows": 0,
        "clean_execution_rows": 60,
        "paper_ready_candidates": 0,
        "best_ev_candidate_ticker": "KXXRP-TEST",
        "best_current_expected_value_cents": "-0.8",
        "best_ev_gap_to_positive_cents": "0.8",
        "exact_snapshot_refresh_selected": 50,
        "exact_snapshot_refresh_repaired": 50,
        "post_refresh_dashboard_truth_status": "REFRESHED",
    }
    status = {
        "generated_at": now,
        "latest_report_generated_at": now,
        "guard": {
            "status": "RUNNING",
            "running": True,
            "stale_report": False,
        },
        "latest_summary": summary,
    }
    watch = {
        "generated_at": now,
        "summary": summary,
    }
    (r5_dir / "phase3bc_r5_status.json").write_text(
        json.dumps(status),
        encoding="utf-8",
    )
    (r5_dir / "phase3bc_r5_crypto_freshness_watch.json").write_text(
        json.dumps(watch),
        encoding="utf-8",
    )


def _write_conflicting_phase3ar_gate(reports_dir: Path) -> None:
    phase3ar_dir = reports_dir / "phase3ar"
    phase3ar_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": utc_now().isoformat(),
        "summary": {
            "paper_ready_rows": 0,
            "positive_ev_rows": 17,
            "positive_ev_no_executable_book_rows": 17,
            "first_hard_blocker": "STALE_CATALOG",
        },
        "positive_ev_rows": [
            {"market_ticker": "KXBTC-26JUL0809-B61950", "primary_blocker": "STALE_CATALOG"}
        ],
    }
    (phase3ar_dir / "paper_ready_gate_after_url_repair.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
