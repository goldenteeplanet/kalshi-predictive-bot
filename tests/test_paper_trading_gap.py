from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.paper_trading_gap import build_paper_trading_gap_analysis


def test_paper_trading_gap_keeps_accelerator_closed_when_ev_not_positive(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_closed_gate_reports(reports_dir)

    payload = build_paper_trading_gap_analysis(
        reports_dir=reports_dir,
        generated_at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert payload["summary"]["market_fill_ready"] is True
    assert payload["summary"]["trade_ranking_ready"] is True
    assert payload["summary"]["paper_trade_ready"] is False
    assert payload["summary"]["accelerate_learning_allowed"] is False
    assert payload["summary"]["current_blocker"] == "EV_NOT_POSITIVE"
    assert payload["phase_statuses"]["positive_ev_gate"]["status"] == "WAITING"
    assert payload["phase_statuses"]["paper_trade_creation"]["status"] == "BLOCKED"
    assert "Do not run accelerate-learning." in payload["do_not_run_yet"]
    assert any(gap["code"] == "SPORTS_DIAGNOSTIC_ONLY" for gap in payload["remaining_gaps"])


def test_paper_trading_gap_opens_operator_review_when_paper_ready(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_closed_gate_reports(reports_dir, positive_ev_rows=2, paper_ready_candidates=1)
    _write_json(
        reports_dir / "phase3bc_r17" / "phase3bc_r17_crypto_liquidity_actionability.json",
        {
            "summary": {
                "positive_ev_rows": 2,
                "paper_ready_candidates": 1,
                "watch_target": "PAPER_READY_REVIEW",
            }
        },
    )

    payload = build_paper_trading_gap_analysis(
        reports_dir=reports_dir,
        generated_at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert payload["summary"]["paper_trade_ready"] is True
    assert payload["summary"]["accelerate_learning_allowed"] is True
    assert payload["summary"]["current_blocker"] == "PAPER_READY_REVIEW"
    assert payload["phase_statuses"]["positive_ev_gate"]["status"] == "DONE"
    assert (
        payload["phase_statuses"]["paper_trade_creation"]["status"]
        == "READY_FOR_OPERATOR_REVIEW"
    )
    assert all(item != "Do not run accelerate-learning." for item in payload["do_not_run_yet"])


def test_paper_trading_gap_uses_r17_blocker_when_book_not_executable(tmp_path) -> None:
    reports_dir = Path(tmp_path) / "reports"
    _write_closed_gate_reports(reports_dir, positive_ev_rows=5, paper_ready_candidates=0)
    _write_json(
        reports_dir / "phase3bc_r17" / "phase3bc_r17_crypto_liquidity_actionability.json",
        {
            "summary": {
                "positive_ev_rows": 5,
                "paper_ready_candidates": 0,
                "watch_target": "WAIT_FOR_EXECUTABLE_BOOK",
            }
        },
    )

    payload = build_paper_trading_gap_analysis(
        reports_dir=reports_dir,
        generated_at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert payload["summary"]["paper_trade_ready"] is False
    assert payload["summary"]["accelerate_learning_allowed"] is False
    assert payload["summary"]["current_blocker"] == "WAIT_FOR_EXECUTABLE_BOOK"
    assert (
        payload["phase_statuses"]["liquidity_and_risk"]["status"]
        == "WAIT_FOR_EXECUTABLE_BOOK"
    )
    assert any(gap["code"] == "WAIT_FOR_EXECUTABLE_BOOK" for gap in payload["remaining_gaps"])


def _write_closed_gate_reports(
    reports_dir: Path,
    *,
    positive_ev_rows: int = 0,
    paper_ready_candidates: int = 0,
) -> None:
    summary = {
        "active_pure_crypto_rows": 1000,
        "current_active_window_rows": 133,
        "expired_crypto_window_rows": 867,
        "snapshot_backlog_status": "COMPLETE",
        "forecast_backlog_status": "COMPLETE",
        "snapshot_stale_rows": 0,
        "forecast_stale_rows": 0,
        "true_ranking_gap_after_repair": 0,
        "missing_or_stale_ranking_rows": 0,
        "positive_ev_rows": positive_ev_rows,
        "positive_ev_preflight_candidates": paper_ready_candidates,
        "clean_execution_rows": 24,
        "risk_ready_rows": paper_ready_candidates,
        "paper_ready_candidates": paper_ready_candidates,
        "primary_gap_after_refresh": "EV_NOT_POSITIVE"
        if positive_ev_rows == 0
        else "PAPER_READY",
        "phase3bc_main_blocker": "WATCH_NO_POSITIVE_EXPECTED_VALUE",
        "watch_state": "WAITING_FOR_POSITIVE_EV"
        if positive_ev_rows == 0
        else "PAPER_READY_REVIEW",
        "best_current_expected_value_cents": "-0.5",
        "best_ev_gap_to_positive_cents": "0.5",
        "best_ev_candidate_ticker": "KXXRP-TEST",
    }
    _write_json(
        reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json",
        {
            "guard": {
                "status": "RUNNING",
                "running": True,
                "stale_report": False,
                **summary,
            },
            "latest_summary": summary,
        },
    )
    _write_json(
        reports_dir / "phase3bc_r5" / "phase3bc_r5_crypto_freshness_watch.json",
        {"summary": summary},
    )
    _write_json(
        reports_dir / "phase3aw" / "current_crypto_funnel.json",
        {
            "r5_running": True,
            "r5_stale_report": False,
            "current_active_crypto_markets": 1000,
            "snapshot_stale_rows": 0,
            "forecast_stale_rows": 0,
            "ranking_gap_after_repair": 0,
            "paper_ready_candidates": paper_ready_candidates,
        },
    )
    _write_json(
        reports_dir / "phase3bc_r16" / "phase3bc_r16_crypto_paper_ready_edge_hunt.json",
        {
            "summary": {
                "positive_ev_rows": positive_ev_rows,
                "paper_ready_candidates": paper_ready_candidates,
                "clean_execution_rows": 24,
            }
        },
    )
    _write_json(
        reports_dir / "phase3ax" / "phase3ax_gap_analysis.json",
        {
            "summary": {
                "diagnostic_only_rows": 1000,
                "safe_exact_repair_rows": 0,
                "phase3z_rows_safe_to_repair": 0,
                "phase3ax_r6_gate": "HOLD_DIAGNOSTIC_ONLY",
            }
        },
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
