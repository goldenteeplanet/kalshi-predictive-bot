import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.paper.models import BUY_YES
from kalshi_predictor.phase3bc_r4 import _render_markdown, build_phase3bc_r4_payload
from kalshi_predictor.utils.time import utc_now


def test_phase3bc_r4_groups_no_positive_ev_and_price_improvement() -> None:
    now = utc_now().isoformat()
    payload = build_phase3bc_r4_payload(
        [
            _row(
                ticker="KXBTC-EV-BLOCKED",
                readiness_status="WATCH_NO_POSITIVE_EXPECTED_VALUE",
                best_price="0.45",
                model_probability="0.40",
                latest_ranking_at=now,
            )
        ],
        risk_by_ticker={},
        phase3bc_summary={},
        thresholds={},
    )

    assert payload["summary"]["active_pure_crypto_rows"] == 1
    assert payload["summary"]["no_positive_ev_rows"] == 1
    assert payload["blocking_gate_counts"]["ev_not_positive"] == 1
    row = payload["no_positive_ev_examples"][0]
    assert row["expected_value"] == "-0.05"
    assert row["price_improvement_needed_for_positive_ev"] == "5.0"
    assert row["phase3n_risk_state"] == "MISSING"


def test_phase3bc_r4_separates_snapshot_stale_from_ranking_gap() -> None:
    now = utc_now()
    stale = (now.replace(microsecond=0) - timedelta(minutes=20)).isoformat()
    fresh = now.isoformat()

    payload = build_phase3bc_r4_payload(
        [
            _row(
                ticker="KXBTC-SNAPSHOT-STALE",
                latest_snapshot_at=stale,
                latest_forecast_at=fresh,
                latest_ranking_at=fresh,
            )
        ],
        risk_by_ticker={},
        phase3bc_summary={},
        thresholds={},
        freshness_minutes=15,
        now=now,
    )

    summary = payload["summary"]
    row = payload["top_blocked_rows"][0]
    assert summary["snapshot_stale_rows"] == 1
    assert summary["true_ranking_gap_after_repair"] == 0
    assert summary["missing_or_stale_ranking_rows"] == 0
    assert payload["freshness_issue_counts"]["SNAPSHOT_STALE"] == 1
    assert payload["snapshot_freshness_rows"][0]["ticker"] == "KXBTC-SNAPSHOT-STALE"
    assert payload["current_window_diagnostics"][0]["ticker"] == "KXBTC-SNAPSHOT-STALE"
    assert row["freshness_issue"] == "SNAPSHOT_STALE"
    assert "SNAPSHOT_STALE" in row["blocker_categories"]
    assert "RANKING_STALE" not in row["blocker_categories"]
    assert row["active_market"] is True
    assert row["market_status"] == "active"
    assert row["structure_status"] == "PURE_CRYPTO"


def test_phase3bc_r4_surfaces_forecast_stale_primary_gap_examples() -> None:
    now = utc_now()
    fresh = now.isoformat()
    stale_forecast = (now.replace(microsecond=0) - timedelta(minutes=22)).isoformat()

    payload = build_phase3bc_r4_payload(
        [
            _row(
                ticker="KXBTC-FORECAST-STALE",
                latest_snapshot_at=fresh,
                latest_forecast_at=stale_forecast,
                latest_ranking_at=fresh,
            )
        ],
        risk_by_ticker={},
        phase3bc_summary={},
        thresholds={},
        freshness_minutes=15,
        now=now,
    )

    summary = payload["summary"]
    assert summary["primary_gap"] == "FORECAST_STALE"
    assert summary["forecast_stale_rows"] == 1
    assert payload["forecast_freshness_rows"][0]["ticker"] == "KXBTC-FORECAST-STALE"
    assert payload["forecast_freshness_examples"][0]["ticker"] == "KXBTC-FORECAST-STALE"
    primary_gap_row = payload["primary_gap_examples"][0]
    assert primary_gap_row["ticker"] == "KXBTC-FORECAST-STALE"
    assert primary_gap_row["freshness_issue"] == "FORECAST_STALE"

    markdown = _render_markdown(payload)
    assert "## Primary Gap Examples" in markdown
    assert "KXBTC-FORECAST-STALE" in markdown
    assert "Bitcoin price range" in markdown


def test_phase3bc_r4_prunes_expired_crypto_windows_from_snapshot_stale_queue() -> None:
    now = datetime(2026, 6, 30, 23, 30, tzinfo=UTC)
    stale = (now - timedelta(minutes=45)).isoformat()

    payload = build_phase3bc_r4_payload(
        [
            _row(
                ticker="KXBTC-26JUN3019-B59050",
                best_price="0.40",
                model_probability="0.60",
                latest_snapshot_at=stale,
                latest_forecast_at=stale,
                latest_ranking_at=stale,
            )
        ],
        risk_by_ticker={},
        phase3bc_summary={},
        thresholds={},
        freshness_minutes=15,
        now=now,
    )

    summary = payload["summary"]
    assert summary["active_pure_crypto_rows"] == 1
    assert summary["current_active_window_rows"] == 0
    assert summary["expired_crypto_window_rows"] == 1
    assert summary["snapshot_stale_rows"] == 0
    assert summary["positive_ev_rows"] == 0
    assert summary["primary_gap"] == "EXPIRED_CRYPTO_WINDOWS_ONLY"
    assert payload["freshness_issue_counts"]["EXPIRED_CRYPTO_WINDOW"] == 1
    assert payload["top_blocked_rows"] == []
    row = payload["expired_crypto_window_examples"][0]
    assert row["freshness_issue"] == "EXPIRED_CRYPTO_WINDOW"
    assert row["active_window_status"] == "EXPIRED"
    assert row["ticker_close_time_utc"] == "2026-06-30T23:00:00+00:00"
    assert "EXPIRED_CRYPTO_WINDOW" in row["blocker_categories"]
    assert "expired_crypto_window" in row["blocking_gates"]
    assert "Expired crypto windows dominate" in payload["recommended_next_action"]


def test_phase3bc_r4_zero_liquidity_is_not_clean_execution() -> None:
    now = utc_now().isoformat()
    payload = build_phase3bc_r4_payload(
        [
            _row(
                ticker="KXBTC-ZERO-LIQUIDITY",
                readiness_status="WATCH_LOW_SCORE",
                best_price="0.40",
                model_probability="0.45",
                liquidity_score="0",
                latest_ranking_at=now,
            )
        ],
        risk_by_ticker={},
        phase3bc_summary={},
        thresholds={},
    )

    assert payload["summary"]["positive_ev_rows"] == 1
    assert payload["summary"]["clean_execution_rows"] == 0


def test_phase3bc_r4_flags_paper_ready_rows_missing_phase3n() -> None:
    payload = build_phase3bc_r4_payload(
        [
            _row(
                ticker="KXETH-PAPER-READY-NO-RISK",
                readiness_status="PAPER_READY_CANDIDATE",
                final_action="PAPER_READY_CANDIDATE",
                best_price="0.40",
                model_probability="0.75",
            )
        ],
        risk_by_ticker={},
        phase3bc_summary={},
        thresholds={},
    )

    assert payload["summary"]["paper_ready_candidates"] == 1
    assert payload["summary"]["missing_phase3n_for_paper_ready_rows"] == 1
    assert payload["paper_ready_missing_risk_rows"][0]["phase3n_risk_state"] == "MISSING"
    assert "Phase 3M/3N" in payload["recommended_next_action"]


def test_phase3bc_r4_uses_existing_phase3n_risk_state() -> None:
    now = utc_now().isoformat()
    payload = build_phase3bc_r4_payload(
        [_row(ticker="KXDOGE-RISK-BLOCKED", latest_ranking_at=now)],
        risk_by_ticker={
            "KXDOGE-RISK-BLOCKED": {
                "id": 7,
                "decision_timestamp": now,
                "action": "BLOCK",
                "mode": "shadow",
            }
        },
        phase3bc_summary={},
        thresholds={},
    )

    assert payload["phase3n_risk_counts"]["BLOCKED"] == 1
    assert payload["top_blocked_rows"][0]["phase3n_latest"]["id"] == 7


def test_phase3bc_r4_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3bc_r4_cli.db'}"
    output_dir = Path(tmp_path) / "phase3bc_r4"
    phase3bc_output_dir = Path(tmp_path) / "phase3bc"

    result = runner.invoke(
        app,
        [
            "phase3bc-r4-crypto-ev-risk-diagnostics",
            "--output-dir",
            str(output_dir),
            "--phase3bc-output-dir",
            str(phase3bc_output_dir),
            "--limit",
            "10",
        ],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "PAPER ONLY" in result.output
    payload_path = output_dir / "phase3bc_r4_crypto_ev_risk_diagnostics.json"
    assert payload_path.exists()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "PAPER_ONLY_CRYPTO_EV_AND_RISK_READINESS_DIAGNOSTICS"
    assert payload["live_or_demo_execution"] is False


def _row(
    *,
    ticker: str,
    readiness_status: str = "WATCH_NO_POSITIVE_EXPECTED_VALUE",
    final_action: str = "WATCH_ONLY",
    best_price: str = "0.50",
    model_probability: str = "0.40",
    latest_snapshot_at: str | None = None,
    latest_forecast_at: str | None = None,
    latest_ranking_at: str | None = None,
    liquidity_score: str = "80",
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "clean_title": "Bitcoin price range",
        "event_ticker": f"{ticker}-EVENT",
        "series_ticker": "KXBTC",
        "active_market": True,
        "market_status": "active",
        "structure_status": "PURE_CRYPTO",
        "readiness_status": readiness_status,
        "final_action": final_action,
        "best_side": BUY_YES,
        "best_price": best_price,
        "model_probability": model_probability,
        "estimated_edge": str(float(model_probability) - float(best_price)),
        "opportunity_score": "65",
        "liquidity_score": liquidity_score,
        "spread": "0.02",
        "confidence_score": "80",
        "time_to_close_minutes": "360",
        "latest_snapshot_at": latest_snapshot_at or latest_ranking_at,
        "latest_forecast_at": latest_forecast_at or latest_ranking_at,
        "latest_ranking_at": latest_ranking_at,
    }
