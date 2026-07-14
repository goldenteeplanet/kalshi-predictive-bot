import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from kalshi_predictor import phase3bc_r3 as r3
from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.jobs.collect_once import CollectOnceSummary
from kalshi_predictor.phase3bc_r3 import (
    _forecast_snapshot_candidates,
    _forecast_snapshots_for_scope,
    _LiquidityHint,
    _select_near_money_candidates,
)
from kalshi_predictor.scheduler import scheduler_plan


def test_crypto_watch_scheduler_runs_phase3bc_r5_every_15_minutes() -> None:
    steps = scheduler_plan("crypto-watch")

    assert len(steps) == 1
    assert steps[0].every_minutes == 15
    assert "phase3bc-r5-unattended-start" in steps[0].command
    assert "--refresh-open-markets" in steps[0].command
    assert "--diagnose-snapshots" in steps[0].command
    assert "--forecast-current-windows-only" in steps[0].command
    assert "--skip-opportunity-report" in steps[0].command
    assert "--crypto-series-tickers KXBTC,KXETH,KXSOLE,KXXRP,KXDOGE" in steps[0].command
    assert "--market-limit 150" in steps[0].command
    assert "--market-max-pages 1" in steps[0].command
    assert "--crypto-market-scan-limit 2500" in steps[0].command
    assert "--crypto-link-limit 500" in steps[0].command
    assert "--opportunity-limit 500" in steps[0].command
    assert "--phase3bc-limit 1000" in steps[0].command
    assert "--near-money-only" in steps[0].command
    assert "--near-money-per-symbol-limit 40" in steps[0].command
    assert "--near-money-window-limit 20" in steps[0].command
    assert "--snapshot-fetch-concurrency 2" in steps[0].command


def test_phase3bc_r3_near_money_selection_skips_expired_and_far_otm() -> None:
    selection = _select_near_money_candidates(
        [
            _market("KXBTC-24JAN0101-B60000"),
            _market("KXBTC-99DEC3123-B59000"),
            _market("KXBTC-99DEC3123-B61000"),
            _market("KXBTC-99DEC3123-B90000"),
            _market("KXETH-99DEC3123-B3000"),
        ],
        latest_prices={"BTC": Decimal("60000"), "ETH": Decimal("3000")},
        per_symbol_limit=2,
        per_window_limit=2,
        now=datetime(2026, 6, 30, tzinfo=UTC),
    )

    selected = selection["selected_candidates"]

    assert [row.ticker for row in selected] == [
        "KXETH-99DEC3123-B3000",
        "KXBTC-99DEC3123-B59000",
        "KXBTC-99DEC3123-B61000",
    ]
    assert selection["skipped_expired_windows"] == 1
    assert selection["skipped_far_otm_rows"] == 1
    assert selection["per_symbol_selected_counts"] == {"ETH": 1, "BTC": 2}


def test_phase3bc_r3_near_money_selection_prefers_current_windows() -> None:
    selection = _select_near_money_candidates(
        [
            _market("KXBTC-99DEC3123-B60000"),
            _market("KXBTC-99JUL0101-B60500"),
        ],
        latest_prices={"BTC": Decimal("60000")},
        per_symbol_limit=1,
        per_window_limit=1,
        now=datetime(2026, 6, 30, tzinfo=UTC),
    )

    selected = selection["selected_candidates"]

    assert [row.ticker for row in selected] == ["KXBTC-99JUL0101-B60500"]


def test_phase3bc_r3_near_money_selection_prioritizes_liquidity_hints() -> None:
    selection = _select_near_money_candidates(
        [
            _market("KXBTC-99DEC3123-B60000"),
            _market("KXBTC-99DEC3123-B61000"),
        ],
        latest_prices={"BTC": Decimal("60000")},
        per_symbol_limit=1,
        per_window_limit=1,
        liquidity_hints={
            "KXBTC-99DEC3123-B61000": _LiquidityHint(
                ticker="KXBTC-99DEC3123-B61000",
                liquidity_score=Decimal("60"),
                spread=Decimal("0.01"),
                source="market_ranking",
                observed_at=datetime(2026, 6, 30, tzinfo=UTC),
            )
        },
        now=datetime(2026, 6, 30, tzinfo=UTC),
    )

    selected = selection["selected_candidates"]

    assert [row.ticker for row in selected] == ["KXBTC-99DEC3123-B61000"]
    assert selection["liquidity_hint_candidates"] == 1
    assert selection["liquidity_first_selected"] == 1
    assert selection["per_symbol_liquidity_first_selected_counts"] == {"BTC": 1}


def test_phase3bc_r3_near_money_selection_keeps_expired_liquidity_hints_blocked() -> None:
    selection = _select_near_money_candidates(
        [
            _market("KXBTC-24JAN0101-B60000"),
            _market("KXBTC-99DEC3123-B60000"),
        ],
        latest_prices={"BTC": Decimal("60000")},
        per_symbol_limit=2,
        per_window_limit=2,
        liquidity_hints={
            "KXBTC-24JAN0101-B60000": _LiquidityHint(
                ticker="KXBTC-24JAN0101-B60000",
                liquidity_score=Decimal("99"),
                spread=Decimal("0.01"),
                source="market_snapshot",
                observed_at=datetime(2026, 6, 30, tzinfo=UTC),
            )
        },
        now=datetime(2026, 6, 30, tzinfo=UTC),
    )

    selected = selection["selected_candidates"]

    assert [row.ticker for row in selected] == ["KXBTC-99DEC3123-B60000"]
    assert selection["skipped_expired_windows"] == 1
    assert selection["liquidity_hint_candidates"] == 0
    assert selection["liquidity_first_selected"] == 0


def test_phase3bc_r3_current_window_scope_skips_expired_crypto_tickers() -> None:
    current, summary = _forecast_snapshots_for_scope(
        [
            SimpleNamespace(ticker="KXBTC-24JAN0101-T100"),
            SimpleNamespace(ticker="KXBTC-99DEC3123-T100"),
            SimpleNamespace(ticker="KXBTC-NOENCODED-CLOSE"),
        ],
        current_windows_only=True,
    )

    assert [row.ticker for row in current] == [
        "KXBTC-99DEC3123-T100",
        "KXBTC-NOENCODED-CLOSE",
    ]
    assert summary["scope"] == "CURRENT_ACTIVE_CRYPTO_WINDOWS"
    assert summary["candidate_snapshots"] == 3
    assert summary["current_window_snapshots"] == 2
    assert summary["expired_window_snapshots_skipped"] == 1
    assert summary["unknown_window_snapshots"] == 1


def test_phase3bc_r3_rate_limit_summary_blocks_partial_data() -> None:
    summary = CollectOnceSummary(
        markets_seen=12,
        snapshots_inserted=4,
        forecasts_inserted=0,
        skipped_forecasts=0,
        db_location="test",
        collection_status="RATE_LIMITED_PARTIAL",
        rate_limit_status="RATE_LIMITED_PARTIAL",
        rate_limited=True,
        data_complete=False,
        rate_limit_details={
            "status": "RATE_LIMITED_PARTIAL",
            "rate_limited": True,
            "request_count": 7,
            "retry_count": 2,
            "rate_limited_count": 2,
            "retry_exhausted_count": 0,
            "total_sleep_seconds": 3.0,
            "rows_fetched_before_limit": 16,
            "endpoints": [
                {
                    "endpoint": "GET /markets",
                    "status_code": 429,
                    "retry_count": 2,
                    "total_sleep_seconds": 3.0,
                    "retry_exhausted": False,
                }
            ],
        },
    )

    rate_limit = r3._rate_limit_summary([summary])
    collect_payload = r3._collect_summary_payload(summary)

    assert rate_limit["status"] == "RATE_LIMITED_PARTIAL"
    assert rate_limit["blocker"] == "RATE_LIMITED_KALSHI_API"
    assert rate_limit["data_complete"] is False
    assert rate_limit["rows_fetched_before_limit"] == 16
    assert rate_limit["top_endpoint"] == "GET /markets"
    assert collect_payload["rate_limited"] is True
    assert collect_payload["data_complete"] is False


def test_phase3bc_r3_near_money_forecasts_collected_tickers(monkeypatch) -> None:
    calls: dict[str, list[str]] = {}

    def fake_latest_snapshots_for_forecasts(session, tickers):
        del session
        calls["tickers"] = list(tickers)
        return [SimpleNamespace(ticker=ticker) for ticker in tickers]

    def fail_latest_snapshots_for_model(*args, **kwargs):
        raise AssertionError("near-money mode should not scan all linked crypto snapshots")

    monkeypatch.setattr(
        r3,
        "latest_snapshots_for_forecasts",
        fake_latest_snapshots_for_forecasts,
    )
    monkeypatch.setattr(r3, "latest_snapshots_for_model", fail_latest_snapshots_for_model)

    snapshots, source = _forecast_snapshot_candidates(
        object(),
        model_name="crypto_v2",
        limit=2,
        near_money_only=True,
        link_tickers=[
            "KXBTC-99DEC3123-B60000",
            "KXETH-99DEC3123-B3000",
            "KXBTC-99DEC3123-B60000",
        ],
    )

    assert source == "COLLECTED_NEAR_MONEY_TICKERS"
    assert calls["tickers"] == ["KXBTC-99DEC3123-B60000", "KXETH-99DEC3123-B3000"]
    assert [row.ticker for row in snapshots] == [
        "KXBTC-99DEC3123-B60000",
        "KXETH-99DEC3123-B3000",
    ]


def test_phase3bc_r3_cli_smoke_no_external_fetches(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3bc_r3_cli.db'}"
    output_dir = Path(tmp_path) / "phase3bc_r3"
    phase3bc_output_dir = Path(tmp_path) / "phase3bc"

    result = runner.invoke(
        app,
        [
            "phase3bc-r3-active-crypto-refresh",
            "--output-dir",
            str(output_dir),
            "--phase3bc-output-dir",
            str(phase3bc_output_dir),
            "--skip-external-crypto-ingest",
            "--skip-open-market-refresh",
            "--diagnose-snapshots",
            "--crypto-series-tickers",
            "KXBTC,KXETH",
            "--crypto-market-scan-limit",
            "25",
            "--crypto-link-limit",
            "10",
            "--forecast-limit",
            "10",
            "--opportunity-limit",
            "10",
            "--phase3bc-limit",
            "10",
        ],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "PAPER ONLY" in result.output
    payload_path = output_dir / "phase3bc_r3_active_crypto_refresh.json"
    assert payload_path.exists()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "PAPER_ONLY_ACTIVE_CRYPTO_REFRESH_AND_RANKING_FRESHNESS"
    assert payload["cadence"]["target_minutes"] == 15
    assert payload["live_or_demo_execution"] is False
    assert payload["crypto_series_tickers"] == ["KXBTC", "KXETH"]
    assert payload["options"]["external_crypto_ingest"] is False
    assert payload["options"]["refresh_open_markets"] is False
    assert payload["options"]["repair_snapshots"] is False
    assert payload["options"]["near_money_only"] is False
    assert payload["options"]["crypto_market_scan_limit"] == 25
    assert payload["options"]["phase3ar_scope"] == "LATEST_CRYPTO_LINKS"
    assert payload["options"]["phase3ar_ticker_count"] is None
    assert (phase3bc_output_dir / "phase3bc_crypto_clean_opportunity_router.json").exists()


def _market(ticker: str, *, status: str = "open") -> dict[str, str]:
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "series_ticker": ticker.split("-", 1)[0],
        "status": status,
        "title": ticker,
    }
