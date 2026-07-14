import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.repository import insert_crypto_features, insert_crypto_market_link
from kalshi_predictor.crypto.semantics import EXACT_LINK, UNSUPPORTED, parse_crypto_market_terms
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import ForecastSkipLog
from kalshi_predictor.forecasting.crypto_v2 import CryptoV2Forecaster
from kalshi_predictor.market_legs import parse_market_legs
from kalshi_predictor.phase3ag_crypto import (
    build_phase3ag_crypto_pipeline,
    write_phase3ag_crypto_report,
)
from kalshi_predictor.utils.time import utc_now


def test_crypto_terms_parse_structured_target_price_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXBTC-TERMS",
                "title": "yes Target Price: $100,000",
                "series_ticker": "KXBTC",
                "rules_primary": "Settles to a public Bitcoin reference price.",
            },
        )
        legs = parse_market_legs(market)

        terms = parse_crypto_market_terms(market, legs=legs)

    assert terms.status == EXACT_LINK
    assert terms.symbol == "BTC"
    assert terms.component_symbols == ("BTC",)
    assert terms.components[0].threshold_value == "100000"
    assert terms.idempotency_key


def test_crypto_terms_reject_unsupported_low_price_asset(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXCRYPTO-UNSUPPORTED",
                "title": "yes Target Price: $0.001",
                "series_ticker": "KXMVECROSSCATEGORY",
            },
        )
        legs = parse_market_legs(market)

        terms = parse_crypto_market_terms(market, legs=legs)

    assert terms.status == UNSUPPORTED
    assert "unsupported_target_price_asset" in terms.reason_codes


def test_crypto_v2_blocks_future_feature_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        captured_at = utc_now()
        snapshot = insert_market_snapshot(
            session,
            {
                "ticker": "KXBTC-FUTURE",
                "status": "open",
                "title": "Will BTC exceed 70000?",
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.50",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"]],
                    "no_dollars": [["0.50", "10"]],
                }
            },
            captured_at,
        )
        insert_crypto_market_link(
            session,
            ticker=snapshot.ticker,
            symbol="BTC",
            confidence="1.0",
            reason="test",
            raw_json={"structured_terms": _btc_terms_payload(snapshot.ticker)},
        )
        insert_crypto_features(
            session,
            symbol="BTC",
            source="test",
            generated_at=captured_at + timedelta(hours=1),
            window_minutes=1440,
            features={
                "price": "100",
                "history_minutes": "120",
                "momentum_score": "0.5",
                "trend_direction": "UP",
            },
        )

        forecast = CryptoV2Forecaster(settings=_settings()).forecast(session, snapshot)
        skip = session.scalar(
            select(ForecastSkipLog).where(ForecastSkipLog.ticker == snapshot.ticker)
        )

    assert forecast is None
    assert skip is not None
    assert "future feature" in skip.reason


def test_crypto_v2_cached_feature_rows_still_block_future_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        captured_at = utc_now()
        snapshot = insert_market_snapshot(
            session,
            {
                "ticker": "KXBTC-FUTURE-CACHED",
                "status": "open",
                "title": "Will BTC exceed 70000?",
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.50",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"]],
                    "no_dollars": [["0.50", "10"]],
                }
            },
            captured_at,
        )
        insert_crypto_market_link(
            session,
            ticker=snapshot.ticker,
            symbol="BTC",
            confidence="1.0",
            reason="test",
            raw_json={"structured_terms": _btc_terms_payload(snapshot.ticker)},
        )
        insert_crypto_features(
            session,
            symbol="BTC",
            source="test",
            generated_at=captured_at + timedelta(hours=1),
            window_minutes=1440,
            features={
                "price": "100",
                "history_minutes": "120",
                "momentum_score": "0.5",
                "trend_direction": "UP",
            },
        )

        forecaster = CryptoV2Forecaster(settings=_settings())
        forecaster.begin_forecast_run()
        try:
            forecast = forecaster.forecast(session, snapshot)
        finally:
            forecaster.end_forecast_run()
        skip = session.scalar(
            select(ForecastSkipLog).where(ForecastSkipLog.ticker == snapshot.ticker)
        )

    assert forecast is None
    assert skip is not None
    assert "future feature" in skip.reason


def test_phase3ag_crypto_report_renders_funnel(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase3ag_crypto"
    with session_factory() as session:
        _seed_ready_crypto_market(session)

        payload = build_phase3ag_crypto_pipeline(session, settings=_settings())
        artifacts = write_phase3ag_crypto_report(
            session,
            output_dir=output_dir,
            settings=_settings(),
        )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")
    report_json = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["paper_only_safety"] == "PAPER_ONLY_NO_EXCHANGE_WRITES"
    assert payload["funnel"]["eligible_crypto_markets"] == 1
    assert "Crypto Funnel" in markdown
    assert report_json["phase"] == "3AG_CRYPTO"


def test_phase3ag_crypto_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'phase3ag_crypto_cli.db'}"
    output_dir = Path(tmp_path) / "reports"

    result = runner.invoke(
        app,
        ["phase3ag-crypto-pipeline", "--output-dir", str(output_dir), "--limit", "10"],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert (output_dir / "phase3ag_crypto_pipeline.json").exists()
    assert "PAPER ONLY" in result.output


def _seed_ready_crypto_market(session) -> None:
    now = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": "KXBTC-READY",
            "status": "open",
            "title": "Will BTC exceed 70000?",
            "series_ticker": "KXBTC",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
        },
        {"orderbook_fp": {"yes_dollars": [["0.40", "10"]], "no_dollars": [["0.50", "10"]]}},
        now,
    )
    insert_crypto_market_link(
        session,
        ticker=snapshot.ticker,
        symbol="BTC",
        confidence="1.0",
        reason="test",
        raw_json={"structured_terms": _btc_terms_payload(snapshot.ticker)},
    )
    insert_crypto_features(
        session,
        symbol="BTC",
        source="test",
        generated_at=now - timedelta(minutes=1),
        window_minutes=1440,
        features={
            "price": "100",
            "history_minutes": "120",
            "momentum_score": "0.5",
            "trend_direction": "UP",
        },
    )


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
        "event_ticker": "KXBTC-TEST",
        "market_type": "binary",
        "idempotency_key": "test-btc-terms",
    }


def _settings() -> Settings:
    return Settings(
        crypto_v2_max_adjustment=Decimal("0.08"),
        crypto_v2_min_link_confidence=Decimal("0.6"),
        crypto_v2_min_history_minutes=60,
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ag_crypto.db'}")
    return get_session_factory(engine)
