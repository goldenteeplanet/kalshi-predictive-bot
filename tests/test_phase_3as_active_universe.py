from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.active_universe import latest_links_for_table
from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.repository import insert_crypto_features, insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    decode_json,
    insert_forecast,
    insert_market_snapshot,
    upsert_market,
)
from kalshi_predictor.data.schema import CryptoMarketLink, MarketOpportunity
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.registry import latest_snapshots_for_model
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.phase3ar import STATUS_READY, build_crypto_forecast_coverage
from kalshi_predictor.phase3as import build_active_market_universe, write_phase3as_report
from kalshi_predictor.sports.repository import insert_sports_market_link
from kalshi_predictor.utils.time import utc_now


def test_phase3as_marks_closed_crypto_and_sports_links_deprecated(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_link(session, "KXBTC-OPEN", status="open")
        closed_link = _seed_crypto_link(
            session,
            "KXBTC-CLOSED",
            status="closed",
            with_snapshot=False,
        )
        _seed_sports_link(session, "KXSPORT-CLOSED", status="settled")

        payload = build_active_market_universe(session, limit=10, mark_deprecated=True)
        session.flush()

        closed_raw = decode_json(closed_link.raw_json)

    assert payload["summary"]["active_linked_markets"] == 1
    assert payload["summary"]["inactive_linked_markets"] == 2
    assert payload["summary"]["deprecated_marked_this_run"] == 2
    assert closed_raw["phase3as_deprecated"] is True
    assert closed_raw["phase3as_deprecated_reason"] == "closed_or_inactive_market"


def test_latest_links_for_table_returns_latest_unique_tickers(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        now = utc_now()
        old = insert_crypto_market_link(
            session,
            ticker="KXBTC-DUPLICATE",
            symbol="BTC",
            confidence="0.5",
            reason="old duplicate",
            detected_at=now - timedelta(minutes=10),
        )
        newest = insert_crypto_market_link(
            session,
            ticker="KXBTC-DUPLICATE",
            symbol="ETH",
            confidence="0.9",
            reason="newest duplicate",
            detected_at=now,
        )
        other = insert_crypto_market_link(
            session,
            ticker="KXETH-OTHER",
            symbol="ETH",
            confidence="0.8",
            reason="other ticker",
            detected_at=now - timedelta(minutes=5),
        )

        links = latest_links_for_table(session, CryptoMarketLink, limit=2)

    assert [link.id for link in links] == [newest.id, other.id]
    assert old.id not in {link.id for link in links}


def test_latest_snapshots_for_model_excludes_closed_linked_markets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        open_snapshot = _seed_crypto_link(session, "KXBTC-ACTIVE", status="open")
        _seed_crypto_link(session, "KXBTC-OLD-CLOSED", status="closed")

        rows = latest_snapshots_for_model(session, model_name="crypto_v2", limit=10)

    assert rows is not None
    assert [row.ticker for row in rows] == [open_snapshot.ticker]


def test_opportunity_scanner_skips_closed_markets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_opportunity_market(session, ticker="KXOPP-CLOSED", status="closed")

        summary = scan_opportunities(
            session,
            settings=_opportunity_settings(),
            min_edge=Decimal("0.01"),
            min_score=Decimal("1"),
        )

    assert summary.markets_scanned == 1
    assert summary.rankings_inserted == 0
    assert summary.opportunities_detected == 0
    with session_factory() as session:
        assert session.scalar(select(MarketOpportunity)) is None


def test_phase3ar_reports_active_crypto_link_counts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        captured_at = utc_now()
        _seed_crypto_link(session, "KXBTC-READY-AS", status="open", captured_at=captured_at)
        _seed_crypto_link(session, "KXBTC-CLOSED-AS", status="closed", with_snapshot=False)
        _seed_crypto_feature(session, generated_at=captured_at - timedelta(minutes=5))

        payload = build_crypto_forecast_coverage(session, settings=_crypto_settings(), limit=10)

    assert payload["summary"]["active_linked_crypto_markets"] == 1
    assert payload["summary"]["closed_or_inactive_linked_crypto_markets"] == 1
    assert payload["summary"]["active_ready_to_forecast"] == 1
    assert any(row["status"] == STATUS_READY for row in payload["rows"])


def test_phase3as_report_and_cli_help(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_crypto_link(session, "KXBTC-REPORT", status="closed")
        artifacts = write_phase3as_report(
            session,
            output_dir=Path(tmp_path) / "phase3as",
            limit=10,
            mark_deprecated=True,
        )

    assert artifacts.markdown_path.exists()
    report = artifacts.markdown_path.read_text(encoding="utf-8")
    assert "Phase 3AS Active Market Universe" in report
    assert "inactive_linked_markets: 1" in report

    runner = CliRunner()
    for command in ("active-universe-doctor", "phase3as-active-universe"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "mark-deprecated" in result.output


def test_phase3as_cli_writes_report(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3as_cli.db'}"
    output_dir = Path(tmp_path) / "reports"

    result = runner.invoke(
        app,
        ["active-universe-doctor", "--output-dir", str(output_dir), "--limit", "10"],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert (output_dir / "phase3as_active_universe.json").exists()
    assert "PAPER ONLY" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3as.db'}")
    return get_session_factory(engine)


def _seed_crypto_link(
    session,
    ticker: str,
    *,
    status: str,
    captured_at=None,
    with_snapshot: bool = True,
):
    now = captured_at or utc_now()
    upsert_market(session, _market_payload(ticker, status=status))
    snapshot = None
    if with_snapshot:
        snapshot = insert_market_snapshot(
            session,
            _market_payload(ticker, status=status),
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"]],
                    "no_dollars": [["0.50", "10"]],
                }
            },
            now,
        )
    link = insert_crypto_market_link(
        session,
        ticker=ticker,
        symbol="BTC",
        confidence="1.0",
        reason="test",
        raw_json={"structured_terms": _btc_terms_payload(ticker)},
    )
    session.flush()
    return snapshot or link


def _seed_sports_link(session, ticker: str, *, status: str):
    upsert_market(session, _market_payload(ticker, status=status))
    insert_sports_market_link(
        session,
        ticker=ticker,
        league="MLB",
        game_key=f"MLB:{ticker.lower()}",
        market_type="MONEYLINE",
        link_confidence=Decimal("0.9"),
        link_reason="test",
        matched_terms=["test"],
        raw_json={"source": "test"},
    )
    session.flush()


def _seed_crypto_feature(session, *, generated_at) -> None:
    insert_crypto_features(
        session,
        symbol="BTC",
        source="test",
        generated_at=generated_at,
        window_minutes=1440,
        features={
            "price": "100",
            "history_minutes": "120",
            "momentum_score": "0.5",
            "trend_direction": "UP",
        },
    )


def _seed_opportunity_market(session, *, ticker: str, status: str) -> None:
    now = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            **_market_payload(ticker, status=status),
            "volume_fp": "1000",
            "open_interest_fp": "500",
            "liquidity_dollars": "10000",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.30", "10"]],
                "no_dollars": [["0.60", "10"]],
            }
        },
        now,
    )
    insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=now,
            model_name="market_implied_v1",
            yes_probability=Decimal("0.70"),
            market_mid_probability=None,
            best_yes_bid=Decimal("0.30"),
            best_yes_ask=Decimal(snapshot.best_yes_ask),
            feature_json={"source": "test"},
        ),
    )
    session.flush()


def _market_payload(ticker: str, *, status: str) -> dict:
    now = utc_now()
    return {
        "ticker": ticker,
        "status": status,
        "title": "Will BTC exceed 70000?",
        "series_ticker": "KXBTC",
        "event_ticker": f"{ticker}-EVENT",
        "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.50",
        "close_time": (now + timedelta(hours=4)).isoformat(),
        "liquidity_dollars": "1000",
    }


def _btc_terms_payload(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "status": "EXACT_LINK",
        "symbol": "BTC",
        "component_symbols": ["BTC"],
        "components": [
            {
                "symbol": "BTC",
                "side": "YES",
                "comparator": "ABOVE",
                "threshold_value": "70000",
                "reference_price_source": "unknown_public_reference",
            }
        ],
        "reason_codes": ["test_terms"],
        "reference_price_source": "unknown_public_reference",
        "observation_time": None,
        "expiration_time": None,
        "settlement_time": None,
        "settlement_timezone": "UTC",
        "settlement_rules": None,
        "series_ticker": "KXBTC",
        "event_ticker": f"{ticker}-EVENT",
        "market_type": "binary",
        "idempotency_key": f"{ticker}:btc:above:70000",
    }


def _crypto_settings() -> Settings:
    return Settings(
        crypto_v2_max_adjustment=Decimal("0.08"),
        crypto_v2_min_link_confidence=Decimal("0.6"),
        crypto_v2_min_history_minutes=60,
    )


def _opportunity_settings() -> Settings:
    return Settings(
        opportunity_min_edge=Decimal("0.01"),
        opportunity_min_score=Decimal("1"),
        opportunity_max_spread=Decimal("0.20"),
        opportunity_min_liquidity=Decimal("0"),
        opportunity_min_time_to_close_minutes=Decimal("30"),
        opportunity_max_results=20,
    )
