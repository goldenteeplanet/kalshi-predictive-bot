import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh4 import (
    GH4_APPROVAL_TOKEN,
    build_gh3_soak_status,
    build_gh4_paper_activation_preflight,
    evaluate_paper_order_activation,
)

NOW = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)


def test_gh3_soak_status_reports_progress_eta_and_source_health(tmp_path: Path) -> None:
    paths = _write_inputs(tmp_path, completed=11, paper_ready_seen=False, current_ready=0)

    status = build_gh3_soak_status(**paths, now=NOW)

    assert status["status"] == "RUNNING"
    assert status["completed_cycles"] == 11
    assert status["remaining_cycles"] == 13
    assert status["eta_label"] == "about 3.2h"
    assert status["paper_ready_seen"] is False
    assert status["reconnect"]["status"] == "HEALTHY"
    assert status["paper_order_creation_enabled"] is False
    assert status["live_execution_enabled"] is False


def test_gh4_preflight_blocks_until_soak_and_candidate_gates_pass(tmp_path: Path) -> None:
    paths = _write_inputs(tmp_path, completed=23, paper_ready_seen=False, current_ready=0)

    payload = build_gh4_paper_activation_preflight(
        settings=Settings(),
        gh2_report_path=paths["report_path"],
        gh2_history_path=paths["history_path"],
        gh1_status_path=paths["gh1_status_path"],
        now=NOW,
    )

    assert payload["status"] == "BLOCKED_GH3_OR_SAFETY_GATES"
    assert payload["preflight_ready"] is False
    assert "gh3_soak_complete" in payload["failed_checks"]
    assert "paper_ready_observed" in payload["failed_checks"]
    assert "current_candidate_available" in payload["failed_checks"]


def test_gh4_preflight_becomes_ready_but_does_not_enable_orders(tmp_path: Path) -> None:
    paths = _write_inputs(tmp_path, completed=24, paper_ready_seen=True, current_ready=1)

    payload = build_gh4_paper_activation_preflight(
        settings=Settings(),
        gh2_report_path=paths["report_path"],
        gh2_history_path=paths["history_path"],
        gh1_status_path=paths["gh1_status_path"],
        now=NOW,
    )

    assert payload["status"] == "READY_FOR_OPERATOR_APPROVAL"
    assert payload["preflight_ready"] is True
    assert payload["activation"]["paper_order_creation_enabled"] is False
    assert payload["activation"]["paper_order_kill_switch"] is True
    assert payload["safety"]["exchange_orders_enabled"] is False


def test_paper_activation_requires_enable_kill_switch_release_and_exact_token(
    tmp_path: Path,
) -> None:
    paths = _write_inputs(tmp_path, completed=24, paper_ready_seen=True, current_ready=1)
    settings = Settings(
        paper_order_creation_enabled=True,
        paper_order_kill_switch=False,
        execution_enabled=False,
        autopilot_enabled=False,
    )
    preflight = build_gh4_paper_activation_preflight(
        settings=settings,
        gh2_report_path=paths["report_path"],
        gh2_history_path=paths["history_path"],
        gh1_status_path=paths["gh1_status_path"],
        now=NOW,
    )

    denied = evaluate_paper_order_activation(
        settings=settings,
        preflight=preflight,
        approval_token="wrong",
    )
    allowed = evaluate_paper_order_activation(
        settings=settings,
        preflight=preflight,
        approval_token=GH4_APPROVAL_TOKEN,
    )

    assert denied["allowed"] is False
    assert "OPERATOR_APPROVAL_TOKEN_MISMATCH" in denied["blockers"]
    assert allowed == {"allowed": True, "blockers": []}


def test_gh4_preflight_blocks_stale_websocket_health(tmp_path: Path) -> None:
    paths = _write_inputs(
        tmp_path,
        completed=24,
        paper_ready_seen=True,
        current_ready=1,
        gh1_generated_at=NOW - timedelta(minutes=8),
    )

    payload = build_gh4_paper_activation_preflight(
        settings=Settings(),
        gh2_report_path=paths["report_path"],
        gh2_history_path=paths["history_path"],
        gh1_status_path=paths["gh1_status_path"],
        now=NOW,
    )

    assert payload["preflight_ready"] is False
    assert "source_reconnect_health" in payload["failed_checks"]


def _write_inputs(
    tmp_path: Path,
    *,
    completed: int,
    paper_ready_seen: bool,
    current_ready: int,
    gh1_generated_at: datetime = NOW,
) -> dict[str, Path]:
    report_path = tmp_path / "gh2.json"
    history_path = tmp_path / "history.jsonl"
    gh1_status_path = tmp_path / "gh1.json"
    report = {
        "generated_at": NOW.isoformat(),
        "status": "PAPER_ONLY_SOAK_RUNNING",
        "errors": [],
        "soak": {
            "healthy_cycle": True,
            "consecutive_healthy_cycles": completed,
            "required_healthy_cycles": 24,
            "paper_ready_seen_in_required_window": paper_ready_seen,
            "soak_complete": completed >= 24 and paper_ready_seen,
        },
        "paper_readiness": {
            "total_paper_ready_candidates": current_ready,
            "crypto_positive_ev_rows": 3,
            "weather_positive_ev_rows": 0,
        },
        "crypto_quote_drain": {
            "status": "COMPLETE",
            "prices_inserted": 5,
            "errors": [],
        },
        "decision_refresh": {
            "fresh_ranked_candidates": 6,
            "weather_features": [{"features_inserted": 4}],
            "weather_forecasts": {"forecasts_inserted": 2},
        },
        "safety": {"paper_orders_created": 0},
    }
    gh1 = {
        "generated_at": gh1_generated_at.isoformat(),
        "state": "STREAMING",
        "snapshots_seen": 50,
        "reconnect_count": 2,
        "consecutive_failures": 0,
    }
    history = {
        "generated_at": NOW.isoformat(),
        "healthy": True,
        "paper_ready_candidates": current_ready,
        "positive_ev_rows": 3,
        "fresh_ranked_candidates": 6,
        "reset_reason": None,
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    gh1_status_path.write_text(json.dumps(gh1), encoding="utf-8")
    history_path.write_text(json.dumps(history) + "\n", encoding="utf-8")
    return {
        "report_path": report_path,
        "history_path": history_path,
        "gh1_status_path": gh1_status_path,
    }
