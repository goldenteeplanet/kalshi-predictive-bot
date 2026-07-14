from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.paper.models import BUY_YES
from kalshi_predictor.phase3bc_r16 import build_phase3bc_r16_payload
from kalshi_predictor.utils.time import utc_now


def test_phase3bc_r16_requires_pure_crypto_rows() -> None:
    payload = build_phase3bc_r16_payload(
        [
            _row("KXBTC-PURE", expected_value="0.08"),
            _row(
                "KXBTC-MIXED",
                expected_value="0.99",
                structure_status="MIXED_CATEGORY",
                readiness_status="BLOCKED_MIXED_CATEGORY",
                final_action="BLOCKED",
            ),
        ],
        phase3bc_payload={"summary": {}},
        r4_payload={"summary": {}},
        now=utc_now(),
    )

    assert payload["summary"]["active_pure_crypto_rows"] == 1
    assert payload["summary"]["current_active_pure_crypto_rows"] == 1
    tickers = {row["ticker"] for row in payload["crypto_decision_rows"]}
    assert tickers == {"KXBTC-PURE"}


def test_phase3bc_r16_ranks_by_executable_ev_before_raw_edge() -> None:
    payload = build_phase3bc_r16_payload(
        [
            _row("KXBTC-HIGH-EDGE-LOW-EV", expected_value="0.02", estimated_edge="0.80"),
            _row("KXBTC-HIGH-EV-LOW-EDGE", expected_value="0.12", estimated_edge="0.03"),
        ],
        phase3bc_payload={"summary": {}},
        r4_payload={"summary": {}},
        now=utc_now(),
    )

    assert payload["best_no_paid_data_rows"][0]["ticker"] == "KXBTC-HIGH-EV-LOW-EDGE"
    assert payload["best_no_paid_data_rows"][0]["rank_basis"] == (
        "expected_value_then_liquidity_then_spread"
    )


def test_phase3bc_r16_surfaces_positive_ev_blockers_and_actions() -> None:
    payload = build_phase3bc_r16_payload(
        [
            _row(
                "KXBTC-POSITIVE-STILL-BLOCKED",
                expected_value="0.05",
                readiness_status="BLOCKED_NO_LIQUIDITY",
                final_action="BLOCKED",
            )
        ],
        phase3bc_payload={"summary": {}},
        r4_payload={
            "summary": {},
            "top_blocked_rows": [
                {
                    "ticker": "KXBTC-POSITIVE-STILL-BLOCKED",
                    "freshness_issue": "FRESH",
                    "blocker_categories": ["LIQUIDITY_BLOCKED"],
                    "blocking_gates": ["liquidity_block"],
                    "phase3n_risk_state": "MISSING",
                    "what_would_make_paper_ready": [
                        "Wait for executable liquidity above the configured threshold."
                    ],
                }
            ],
        },
        now=utc_now(),
    )

    row = payload["positive_ev_blocked_rows"][0]
    assert row["primary_blocker"] == "LIQUIDITY_BLOCKED"
    assert row["execution_quality"] == "NO_LIQUIDITY"
    assert "Wait for executable liquidity" in row["what_would_make_paper_ready"][0]


def test_phase3bc_r16_calls_out_expired_active_rows() -> None:
    payload = build_phase3bc_r16_payload(
        [_row("KXBTC-26JUN3019-B59050", expected_value="0.12")],
        phase3bc_payload={"summary": {}},
        r4_payload={"summary": {}},
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert payload["summary"]["active_pure_crypto_rows"] == 1
    assert payload["summary"]["current_active_pure_crypto_rows"] == 0
    assert payload["summary"]["expired_active_pure_crypto_rows"] == 1
    assert "expired windows" in payload["recommended_next_action"]


def test_phase3bc_r16_keeps_paper_only_safety_flags() -> None:
    payload = build_phase3bc_r16_payload(
        [
            _row(
                "KXBTC-READY",
                readiness_status="PAPER_READY_CANDIDATE",
                final_action="PAPER_READY_CANDIDATE",
            )
        ],
        phase3bc_payload={"summary": {}},
        r4_payload={"summary": {}},
        run_refresh=False,
        now=utc_now(),
    )

    assert payload["live_or_demo_execution"] is False
    assert payload["order_submission"] is False
    assert payload["order_cancel_replace"] is False
    assert payload["summary"]["phase3m_phase3n_preflight_attempted"] == 0
    assert payload["paper_ready_rows"][0]["primary_blocker"] == "RISK_MISSING"


def test_phase3bc_r16_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3bc_r16_cli.db'}"
    output_dir = Path(tmp_path) / "phase3bc_r16"

    result = runner.invoke(
        app,
        [
            "phase3bc-r16-crypto-paper-ready-edge-hunt",
            "--output-dir",
            str(output_dir),
            "--limit",
            "10",
        ],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "PAPER ONLY" in result.output
    payload_path = output_dir / "phase3bc_r16_crypto_paper_ready_edge_hunt.json"
    assert payload_path.exists()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "PAPER_ONLY_NO_PAID_DATA_CRYPTO_EDGE_HUNT"
    assert payload["live_or_demo_execution"] is False


def _row(
    ticker: str,
    *,
    expected_value: str = "0.04",
    estimated_edge: str = "0.10",
    structure_status: str = "PURE_CRYPTO",
    readiness_status: str = "WATCH_LOW_SCORE",
    final_action: str = "WATCH_ONLY",
) -> dict[str, object]:
    now = utc_now().isoformat()
    return {
        "ticker": ticker,
        "clean_title": "Bitcoin price range",
        "event_ticker": f"{ticker}-EVENT",
        "series_ticker": "KXBTC",
        "active_market": True,
        "market_status": "active",
        "structure_status": structure_status,
        "readiness_status": readiness_status,
        "final_action": final_action,
        "best_side": BUY_YES,
        "best_price": "0.40",
        "model_probability": "0.52",
        "expected_value": expected_value,
        "estimated_edge": estimated_edge,
        "opportunity_score": "65",
        "liquidity_score": "60",
        "spread": "0.01",
        "confidence_score": "70",
        "time_to_close_minutes": "240",
        "latest_snapshot_at": now,
        "latest_forecast_at": now,
        "latest_ranking_at": now,
        "blockers": ["test blocker"],
        "what_would_make_tradable": ["test action"],
    }
