import json
from pathlib import Path

from kalshi_predictor.phase3an_crypto_source_quality import (
    build_phase3an_crypto_source_quality,
)


def test_source_quality_flags_missing_sol_without_rate_limit(tmp_path) -> None:
    reports_dir = tmp_path / "reports"
    _write_r5_artifacts(
        reports_dir,
        snapshot_counts={"BTC": 20, "ETH": 40, "XRP": 40, "DOGE": 40},
        liquidity_counts={"BTC": 20, "ETH": 40, "XRP": 40, "DOGE": 40},
    )

    payload = build_phase3an_crypto_source_quality(
        output_dir=reports_dir / "phase3an",
        reports_dir=reports_dir,
        symbols="BTC,ETH,SOL,XRP,DOGE",
    )

    summary = payload["summary"]
    assert summary["classification"] == "SOURCE_SERIES_EMPTY"
    assert summary["missing_symbols"] == ["SOL"]
    assert summary["zero_market_symbols"] == ["SOL"]
    assert summary["rate_limit_pressure"] is False
    assert payload["live_or_demo_execution"] is False
    assert payload["order_submission"] is False
    series_by_symbol = {row["symbol"]: row for row in payload["series_refreshes"]}
    assert series_by_symbol["SOL"]["series_ticker"] == "KXSOLE"
    assert series_by_symbol["SOL"]["markets_seen"] == 0
    assert series_by_symbol["SOL"]["status"] == "ZERO_MARKETS_OR_SNAPSHOTS"


def test_source_quality_rate_limit_pressure_beats_coverage(tmp_path) -> None:
    reports_dir = tmp_path / "reports"
    _write_r5_artifacts(
        reports_dir,
        snapshot_counts={"BTC": 1, "ETH": 1, "SOL": 1, "XRP": 1, "DOGE": 1},
        liquidity_counts={"BTC": 1, "ETH": 1, "SOL": 1, "XRP": 1, "DOGE": 1},
        rate_limited=True,
        stdout="fetch failed with HTTP 429 rate limit\n",
    )

    payload = build_phase3an_crypto_source_quality(
        output_dir=reports_dir / "phase3an",
        reports_dir=reports_dir,
        symbols="BTC,ETH,SOL,XRP,DOGE",
    )

    assert payload["summary"]["classification"] == "API_RATE_LIMIT_PRESSURE"
    assert payload["summary"]["rate_limit_pressure"] is True
    assert payload["rate_limit"]["structured_evidence"]
    assert payload["rate_limit"]["log_evidence"]


def test_source_quality_clean_zero_positive_ev_waits_for_market_ev(tmp_path) -> None:
    reports_dir = tmp_path / "reports"
    _write_r5_artifacts(
        reports_dir,
        snapshot_counts={"BTC": 1, "ETH": 1, "SOL": 1, "XRP": 1, "DOGE": 1},
        liquidity_counts={"BTC": 1, "ETH": 1, "SOL": 1, "XRP": 1, "DOGE": 1},
    )

    payload = build_phase3an_crypto_source_quality(
        output_dir=reports_dir / "phase3an",
        reports_dir=reports_dir,
        symbols="BTC,ETH,SOL,XRP,DOGE",
    )

    assert payload["summary"]["classification"] == "WAIT_FOR_MARKET_EV"
    assert payload["summary"]["market_fill_ready"] is True
    assert payload["summary"]["trade_ranking_ready"] is True


def _write_r5_artifacts(
    reports_dir: Path,
    *,
    snapshot_counts: dict[str, int],
    liquidity_counts: dict[str, int],
    rate_limited: bool = False,
    stdout: str = "Completed Phase 3BC-R5 crypto freshness watch cycle 1/1\n",
) -> None:
    r5_dir = reports_dir / "phase3bc_r5"
    r5_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        r5_dir / "phase3bc_r5_status.json",
        {
            "guard": {
                "paper_ready_candidates": 0,
                "positive_ev_rows": 0,
                "snapshot_stale_rows": 0,
                "forecast_stale_rows": 0,
                "true_ranking_gap_after_repair": 0,
                "missing_or_stale_ranking_rows": 0,
                "watch_state": "WAITING_FOR_POSITIVE_EV",
                "primary_gap_after_refresh": "EV_NOT_POSITIVE",
            },
            "latest_summary": {
                "paper_ready_candidates": 0,
                "positive_ev_rows": 0,
                "best_ev_candidate_ticker": "KXBTC-TEST",
                "best_current_expected_value_cents": "-0.5",
            },
            "latest_slowest_stage": {
                "stage": "phase3bc_r3_refresh",
                "duration_seconds": "1.0",
            },
        },
    )
    _write_json(
        r5_dir / "phase3bc_r5_crypto_freshness_watch.json",
        {
            "options": {"symbols": "BTC,ETH,SOL,XRP,DOGE"},
            "summary": {
                "paper_ready_candidates": 0,
                "positive_ev_rows": 0,
            },
            "phase3bc_r3_summary": {
                "per_symbol_snapshot_counts": snapshot_counts,
                "per_symbol_liquidity_first_counts": liquidity_counts,
                "crypto_series_tickers": ["KXBTC", "KXETH", "KXSOLE", "KXXRP", "KXDOGE"],
                "crypto_series_refreshes": [
                    {
                        "markets_seen": snapshot_counts.get(symbol, 0),
                        "snapshots_inserted": snapshot_counts.get(symbol, 0),
                        "collection_status": "COMPLETE",
                        "rate_limit_status": "COMPLETE",
                        "rate_limited": False,
                        "data_complete": True,
                        "per_symbol_snapshot_counts": (
                            {symbol: snapshot_counts[symbol]}
                            if snapshot_counts.get(symbol, 0) > 0
                            else {}
                        ),
                        "per_symbol_liquidity_first_counts": (
                            {symbol: liquidity_counts[symbol]}
                            if liquidity_counts.get(symbol, 0) > 0
                            else {}
                        ),
                    }
                    for symbol in ("BTC", "ETH", "SOL", "XRP", "DOGE")
                ],
                "rate_limit_details": {
                    "status": "RATE_LIMITED" if rate_limited else "COMPLETE",
                    "rate_limited": rate_limited,
                    "rate_limited_count": 1 if rate_limited else 0,
                    "retry_exhausted_count": 0,
                },
            },
        },
    )
    (r5_dir / "phase3bc_r5_unattended_stdout.log").write_text(stdout, encoding="utf-8")
    (r5_dir / "phase3bc_r5_unattended_stderr.log").write_text("", encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
