from __future__ import annotations

from kalshi_predictor.paper.models import BUY_YES
from kalshi_predictor.phase3bc_r17 import build_phase3bc_r17_payload
from kalshi_predictor.utils.time import utc_now


def test_phase3bc_r17_separates_positive_ev_no_book_from_clean_book() -> None:
    payload = build_phase3bc_r17_payload(
        {
            "summary": {"refresh_mode": "R4_DIAGNOSTIC_ONLY"},
            "crypto_decision_rows": [
                _row("KXETH-NO-BOOK", expected_value="0.03", liquidity_score="0"),
                _row("KXBTC-THIN", expected_value="0.02", liquidity_score="10"),
                _row("KXDOGE-CLEAN", expected_value="0.01", liquidity_score="65"),
                _row("KXXRP-NO-EV", expected_value="-0.01", liquidity_score="65"),
            ],
        }
    )

    summary = payload["summary"]
    assert summary["positive_ev_rows"] == 3
    assert summary["positive_ev_no_executable_book_rows"] == 1
    assert summary["positive_ev_thin_book_rows"] == 1
    assert summary["positive_ev_clean_book_rows"] == 1
    assert summary["liquidity_positive_candidates"] == 2
    assert summary["watch_target"] == "RUN_PAPER_ONLY_RISK_PREFLIGHT"
    assert payload["positive_ev_no_executable_book_rows"][0]["ticker"] == "KXETH-NO-BOOK"


def test_phase3bc_r17_paper_ready_wins_actionability() -> None:
    payload = build_phase3bc_r17_payload(
        {
            "summary": {"refresh_mode": "R5_REFRESH_RANKING_AND_PREFLIGHT"},
            "crypto_decision_rows": [
                _row(
                    "KXBTC-READY",
                    expected_value="0.04",
                    liquidity_score="70",
                    paper_ready_candidate=True,
                    phase3n_risk_state="ALLOW",
                )
            ],
        }
    )

    assert payload["summary"]["paper_ready_candidates"] == 1
    assert payload["summary"]["watch_target"] == "PAPER_READY_REVIEW"
    assert payload["paper_ready_rows"][0]["actionability_state"] == "PAPER_READY_CANDIDATE"
    assert payload["live_or_demo_execution"] is False
    assert payload["order_submission"] is False
    assert payload["order_cancel_replace"] is False


def test_phase3bc_r17_keeps_waiting_when_ev_is_not_positive() -> None:
    payload = build_phase3bc_r17_payload(
        {
            "summary": {},
            "crypto_decision_rows": [
                _row("KXBTC-NO-EV", expected_value="-0.01", liquidity_score="80")
            ],
        }
    )

    assert payload["summary"]["positive_ev_rows"] == 0
    assert payload["summary"]["watch_target"] == "WAIT_FOR_POSITIVE_EV"
    assert "15-minute crypto watch" in payload["recommended_next_action"]


def _row(
    ticker: str,
    *,
    expected_value: str,
    liquidity_score: str,
    spread: str = "0.01",
    paper_ready_candidate: bool = False,
    phase3n_risk_state: str = "MISSING",
) -> dict[str, object]:
    now = utc_now().isoformat()
    return {
        "ticker": ticker,
        "clean_title": "Bitcoin price range",
        "event_ticker": f"{ticker}-EVENT",
        "series_ticker": "KXBTC",
        "active_market": True,
        "market_status": "active",
        "structure_status": "PURE_CRYPTO",
        "readiness_status": "PAPER_READY_CANDIDATE"
        if paper_ready_candidate
        else "WATCH_LOW_SCORE",
        "final_action": "PAPER_READY_CANDIDATE" if paper_ready_candidate else "WATCH_ONLY",
        "paper_ready_candidate": paper_ready_candidate,
        "best_side": BUY_YES,
        "best_price": "0.40",
        "model_probability": "0.52",
        "expected_value": expected_value,
        "estimated_edge": "0.12",
        "opportunity_score": "70",
        "liquidity_score": liquidity_score,
        "spread": spread,
        "confidence_score": "70",
        "phase3n_risk_state": phase3n_risk_state,
        "latest_snapshot_at": now,
        "latest_forecast_at": now,
        "latest_ranking_at": now,
    }
