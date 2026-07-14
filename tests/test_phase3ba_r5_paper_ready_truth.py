from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_r5
from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.utils.time import utc_now


def test_phase3ba_r5_crypto_blocker_splits_execution_reasons() -> None:
    assert (
        phase3ba_r5._crypto_blocker(
            {
                "watch_state": "POSITIVE_EV_NO_BOOK",
                "execution_blocker_detail": "ZERO_VISIBLE_DEPTH",
            }
        )
        == "ZERO_VISIBLE_DEPTH"
    )
    assert (
        phase3ba_r5._crypto_blocker(
            {
                "watch_state": "POSITIVE_EV_NO_BOOK",
                "execution_blocker_detail": "ORDERBOOK_MISSING",
            }
        )
        == "EXECUTABLE_BOOK_MISSING"
    )
    assert (
        phase3ba_r5._crypto_blocker({"watch_state": "POSITIVE_EV_THIN_BOOK"})
        == "LIQUIDITY_TOO_LOW"
    )
    assert (
        phase3ba_r5._crypto_blocker({"watch_state": "POSITIVE_EV_WIDE_SPREAD"})
        == "SPREAD_TOO_WIDE"
    )


def test_phase3ba_r5_classifies_older_3ap_as_historical_stale(tmp_path) -> None:
    reports_dir = tmp_path / "reports"
    gate_dir = reports_dir / "phase3ap"
    gate_dir.mkdir(parents=True)
    now = utc_now()
    generated_at = now - timedelta(hours=2)
    (gate_dir / "paper_ready_gate.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at.isoformat(),
                "summary": {
                    "paper_ready_rows": 0,
                    "positive_ev_rows": 0,
                    "first_hard_blocker": "NO_CURRENT_POSITIVE_EV",
                },
            }
        ),
        encoding="utf-8",
    )

    status = phase3ba_r5._classify_3ap_gate(
        reports_dir=reports_dir,
        now=now,
        freshest_trusted_at=now - timedelta(minutes=1),
    )

    assert status["freshness"] == "HISTORICAL_STALE"
    assert status["stale_artifact_not_used_as_current_truth"] is True
    assert status["summary"]["paper_ready_rows"] == 0


def test_phase3ba_r5_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-r5-paper-ready-truth", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-r5-paper-ready-truth" in result.output


def test_phase3ba_r5_uses_r5_aggregate_crypto_truth_when_r4_rows_missing(tmp_path) -> None:
    reports_dir = tmp_path / "reports"
    _write_r5_status(reports_dir, positive_ev_rows=4, no_book_rows=4)
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        payload = phase3ba_r5.build_phase3ba_r5_paper_ready_truth(
            session,
            output_dir=reports_dir / "phase3ba_r5",
            reports_dir=reports_dir,
            max_duration_seconds=120,
        )

    crypto = payload["category_summaries"]["crypto"]
    weather = payload["category_summaries"]["weather"]
    assert crypto["current_rows"] == 4
    assert crypto["source"] == "R5_AGGREGATE_TRUTH_ONLY"
    assert crypto["evidence_scope"] == "R5_AGGREGATE_TRUTH_ONLY"
    assert crypto["positive_ev_rows"] == 4
    assert crypto["paper_ready_rows"] == 0
    assert crypto["first_blocker"] == "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    assert crypto["first_blocker"] != "NO_CURRENT_ROWS"
    assert payload["summary"]["positive_ev_rows"] == 4
    assert payload["summary"]["paper_ready_rows"] == 0
    assert payload["summary"]["first_hard_blocker"] != "PAPER_READY"
    assert payload["summary"]["first_hard_blocker"] == "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    assert weather["first_blocker"] == "WEATHER_TRUTH_MISSING"
    assert "WEATHER_RANKING_REPORT_MISSING" in weather["blocker_counts"]
    assert payload["blocked_rows"][0]["evidence_scope"] == "R5_AGGREGATE_TRUTH_ONLY"
    assert payload["reconciliation_sources"]["crypto"]["selected_source"] == "R5_STATUS_JSON"


def test_phase3ba_r5_r5_positive_ev_headline_beats_weather_backlog(tmp_path) -> None:
    reports_dir = tmp_path / "reports"
    _write_r5_status(reports_dir, positive_ev_rows=4, no_book_rows=4)
    _write_weather_handoff(reports_dir, ranking_missing_rows=10)
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        payload = phase3ba_r5.build_phase3ba_r5_paper_ready_truth(
            session,
            output_dir=reports_dir / "phase3ba_r5",
            reports_dir=reports_dir,
            max_duration_seconds=120,
        )

    summary = payload["summary"]
    assert payload["category_summaries"]["crypto"]["positive_ev_rows"] == 4
    assert payload["category_summaries"]["crypto"]["paper_ready_rows"] == 0
    assert payload["category_summaries"]["weather"]["first_blocker"] == "RANKING_MISSING"
    assert summary["blocker_counts"]["RANKING_MISSING"] == 10
    assert summary["blocker_counts"]["POSITIVE_EV_NO_EXECUTABLE_BOOK"] == 4
    assert summary["first_hard_blocker"] == "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    assert summary["first_hard_blocker"] != "PAPER_READY"
    assert summary["first_hard_blocker"] != "NO_CURRENT_ROWS"


def test_phase3ba_r5_writes_reconciliation_sources(tmp_path) -> None:
    reports_dir = tmp_path / "reports"
    _write_r5_status(reports_dir, positive_ev_rows=4, no_book_rows=4)
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        artifacts = phase3ba_r5.write_phase3ba_r5_paper_ready_truth_report(
            session,
            output_dir=reports_dir / "phase3ba_r5",
            reports_dir=reports_dir,
            max_duration_seconds=120,
        )

    assert artifacts.reconciliation_sources_path.exists()
    sources = json.loads(artifacts.reconciliation_sources_path.read_text(encoding="utf-8"))
    assert sources["crypto"]["selected_source"] == "R5_STATUS_JSON"
    assert sources["weather"]["selected_source"] == "WEATHER_TRUTH_MISSING"


def test_phase3ba_r5_missing_weather_truth_never_reports_paper_ready(tmp_path) -> None:
    reports_dir = tmp_path / "reports"
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        payload = phase3ba_r5.build_phase3ba_r5_paper_ready_truth(
            session,
            output_dir=reports_dir / "phase3ba_r5",
            reports_dir=reports_dir,
            max_duration_seconds=120,
        )

    assert payload["summary"]["paper_ready_rows"] == 0
    assert payload["summary"]["first_hard_blocker"] != "PAPER_READY"
    assert payload["category_summaries"]["weather"]["first_blocker"] == "WEATHER_TRUTH_MISSING"
    assert (
        "WEATHER_RANKING_REPORT_MISSING"
        in payload["category_summaries"]["weather"]["blocker_counts"]
    )


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ba_r5.db'}")
    return get_session_factory(engine)


def _write_r5_status(reports_dir: Path, *, positive_ev_rows: int, no_book_rows: int) -> None:
    status_dir = reports_dir / "phase3bc_r5"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "phase3bc_r5_status.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "latest_watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
                "history_rows": 10,
                "guard": {
                    "running": True,
                    "status": "RUNNING",
                    "positive_ev_rows": positive_ev_rows,
                    "paper_ready_candidates": 0,
                },
                "latest_summary": {
                    "watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
                    "positive_ev_rows": positive_ev_rows,
                    "paper_ready_candidates": 0,
                    "positive_ev_no_executable_book_rows": no_book_rows,
                    "clean_execution_rows": 0,
                    "risk_ready_rows": 0,
                    "primary_gap_after_refresh": "LOW_EDGE_OR_SCORE_BLOCK",
                    "best_ev_candidate_ticker": "KXBTC-TEST",
                    "best_current_expected_value_cents": "1.2",
                },
            }
        ),
        encoding="utf-8",
    )


def _write_weather_handoff(reports_dir: Path, *, ranking_missing_rows: int) -> None:
    weather_dir = reports_dir / "phase3az_r13_weather"
    weather_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ticker": f"KXTEMPNYCH-TEST-{idx}",
            "market_title": "New York high temperature?",
            "market_status": "open",
            "current_window_eligible": True,
            "link_detected_at": utc_now().isoformat(),
            "has_snapshot": True,
            "has_current_forecast": True,
            "has_current_ranking": False,
        }
        for idx in range(ranking_missing_rows)
    ]
    (weather_dir / "weather_handoff_status.json").write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "status": "CURRENT_WEATHER_HANDOFF",
                "handoff_rows": rows,
            }
        ),
        encoding="utf-8",
    )
