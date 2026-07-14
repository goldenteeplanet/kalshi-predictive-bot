from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_r4
from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.utils.time import utc_now


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        opportunity_max_spread=phase3ba_r4.Decimal("0.02"),
        opportunity_min_score=phase3ba_r4.Decimal("60"),
        opportunity_min_edge=phase3ba_r4.Decimal("0.01"),
        opportunity_min_time_to_close_minutes=phase3ba_r4.Decimal("5"),
    )


def _base_row() -> dict:
    return {
        "best_side": "BUY_YES",
        "best_price": "0.40",
        "book_state": "CLEAN_BOOK",
        "book_usable": True,
        "book_reason": "Executable book passes configured gates.",
    }


def test_phase3ba_r4_execution_assessment_splits_book_states() -> None:
    settings = _settings()
    common = {
        "identity": {"tradeable": True, "r4_url_gate_pass": True},
        "snapshot": SimpleNamespace(raw_orderbook_json='{"yes": []}'),
        "liquidity_score": phase3ba_r4.Decimal("40"),
        "spread": phase3ba_r4.Decimal("0.01"),
        "score": phase3ba_r4.Decimal("80"),
        "confidence": phase3ba_r4.Decimal("80"),
        "time_to_close": phase3ba_r4.Decimal("30"),
        "phase3m_contracts": 0,
        "risk_approved": False,
        "settings": settings,
    }

    no_book = phase3ba_r4._execution_assessment(
        {**_base_row(), "book_state": "NO_EXECUTABLE_BOOK", "book_usable": False},
        **common,
    )
    thin = phase3ba_r4._execution_assessment(
        {**_base_row(), "book_state": "THIN_BOOK", "book_usable": True},
        **{**common, "liquidity_score": phase3ba_r4.Decimal("1")},
    )
    wide = phase3ba_r4._execution_assessment(
        {**_base_row(), "book_state": "WIDE_SPREAD", "book_usable": True},
        **{**common, "spread": phase3ba_r4.Decimal("0.10")},
    )
    ready_for_risk = phase3ba_r4._execution_assessment(_base_row(), **common)
    paper_ready = phase3ba_r4._execution_assessment(
        _base_row(),
        **{**common, "phase3m_contracts": 1, "risk_approved": True},
    )

    assert no_book["watch_state"] == "POSITIVE_EV_NO_BOOK"
    assert thin["watch_state"] == "POSITIVE_EV_THIN_BOOK"
    assert wide["watch_state"] == "POSITIVE_EV_WIDE_SPREAD"
    assert ready_for_risk["watch_state"] == "POSITIVE_EV_READY_FOR_RISK"
    assert paper_ready["watch_state"] == "PAPER_READY"


def test_phase3ba_r4_summary_counts_watch_states() -> None:
    rows = [
        {"watch_state": "POSITIVE_EV_NO_BOOK", "execution_blocker_detail": "ORDERBOOK_MISSING"},
        {
            "watch_state": "POSITIVE_EV_THIN_BOOK",
            "execution_blocker_detail": "LIQUIDITY_BELOW_THRESHOLD",
        },
        {
            "watch_state": "PAPER_READY",
            "execution_blocker_detail": "ALL_EXECUTION_AND_RISK_GATES_PASS",
        },
    ]

    summary = phase3ba_r4._summary(rows, liquidity_watchlist=rows[:2])

    assert summary["positive_ev_rows"] == 3
    assert summary["positive_ev_no_book_rows"] == 1
    assert summary["positive_ev_thin_book_rows"] == 1
    assert summary["paper_ready_rows"] == 1
    assert summary["exact_execution_blockers_reported"] is True


def test_phase3ba_r4_exact_catalog_rows_continue_to_book_gate() -> None:
    assert phase3ba_r4._url_gate_pass({"kalshi_url_status": "BUILT_FROM_EXACT_CATALOG"})
    assert not phase3ba_r4._url_gate_pass({"kalshi_url_status": "MALFORMED_URL"})


def test_phase3ba_r4_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-r4-crypto-executable-book-watch", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-r4-crypto-executable-book-watch" in result.output


def test_phase3ba_r4_uses_r5_aggregate_positive_ev_when_rows_missing(
    tmp_path,
    monkeypatch,
) -> None:
    reports_dir = tmp_path / "reports"
    _write_r5_status(reports_dir, positive_ev_rows=4, no_book_rows=4)
    monkeypatch.setattr(
        phase3ba_r4,
        "build_phase3bc_crypto_clean_opportunity_router",
        lambda *_, **__: {"rows": [], "summary": {"test": "no materialized rows"}},
    )
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        payload = phase3ba_r4.build_phase3ba_r4_crypto_executable_book_watch(
            session,
            output_dir=reports_dir / "phase3ba_r4",
            reports_dir=reports_dir,
            limit=100,
        )

    assert payload["summary"]["positive_ev_rows"] == 4
    assert payload["summary"]["paper_ready_rows"] == 0
    assert payload["summary"]["positive_ev_no_executable_book_rows"] == 4
    assert payload["status"] == "CRYPTO_POSITIVE_EV_BLOCKED_BY_EXECUTABLE_BOOK"
    assert payload["status"] != "CRYPTO_WAITING_FOR_POSITIVE_EV"
    row = payload["positive_ev_rows"][0]
    assert row["ticker"] is None
    assert row["evidence_scope"] == "R5_AGGREGATE_TRUTH_ONLY"
    assert row["source"] == "R5_STATUS_JSON"
    assert row["primary_gap_after_refresh"] == "LOW_EDGE_OR_SCORE_BLOCK"
    assert payload["reconciliation_sources"]["crypto"]["selected_source"] == "R5_STATUS_JSON"


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ba_r4.db'}")
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
