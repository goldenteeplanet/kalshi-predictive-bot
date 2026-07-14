from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.features import calculate_crypto_features
from kalshi_predictor.crypto.linker import detect_crypto_market
from kalshi_predictor.crypto.providers import parse_coinbase_spot_response
from kalshi_predictor.crypto.reports import generate_crypto_backtest_report
from kalshi_predictor.crypto.repository import (
    get_crypto_prices,
    insert_crypto_features,
    insert_crypto_market_link,
    insert_crypto_price,
)
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import encode_json, insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import ForecastSkipLog, MarketLeg
from kalshi_predictor.forecasting.crypto_v2 import CryptoV2Forecaster
from kalshi_predictor.utils.time import utc_now


def test_coinbase_provider_response_parsing() -> None:
    quote = parse_coinbase_spot_response(
        "BTC",
        {"data": {"base": "BTC", "currency": "USD", "amount": "65000.12"}},
    )

    assert quote.symbol == "BTC"
    assert quote.source == "coinbase"
    assert quote.price_usd == Decimal("65000.12")


def test_crypto_feature_builder_handles_insufficient_history(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        insert_crypto_price(
            session,
            symbol="BTC",
            source="test",
            observed_at=utc_now(),
            price_usd="100",
        )

        features = calculate_crypto_features(get_crypto_prices(session, "BTC"))

        assert features["return_1h"] is None
        assert features["trend_direction"] == "UNKNOWN"
        assert "Insufficient history" in " ".join(features["notes"])


def test_crypto_feature_builder_calculates_returns_correctly(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        now = utc_now()
        insert_crypto_price(
            session,
            symbol="BTC",
            source="test",
            observed_at=now - timedelta(hours=1),
            price_usd="100",
        )
        insert_crypto_price(
            session,
            symbol="BTC",
            source="test",
            observed_at=now,
            price_usd="110",
        )

        features = calculate_crypto_features(get_crypto_prices(session, "BTC"))

        assert Decimal(features["return_1h"]) == Decimal("0.1")
        assert features["history_minutes"] == 60
        assert features["trend_direction"] == "UP"


def test_linker_detects_btc_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(session, {"ticker": "BTC-TEST", "title": "Will BTC exceed 70000?"})

        symbol, confidence, _reason = detect_crypto_market(market)

        assert symbol == "BTC"
        assert confidence == Decimal("1.0")


def test_linker_detects_eth_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(session, {"ticker": "CRYPTO-TEST", "title": "Ethereum above 4000"})

        symbol, confidence, _reason = detect_crypto_market(market)

        assert symbol == "ETH"
        assert confidence == Decimal("0.8")


def test_linker_ignores_non_crypto_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(session, {"ticker": "WEATHER", "title": "Will it rain?"})

        symbol, confidence, _reason = detect_crypto_market(market)

        assert symbol is None
        assert confidence == Decimal("0.0")


def test_linker_does_not_treat_solana_player_name_as_crypto(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-SOLANA-SIERRA",
                "title": "yes Iva Jovic,yes Solana Sierra,yes Katerina Siniakova",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            },
        )

        symbol, confidence, reason = detect_crypto_market(market)

    assert symbol is None
    assert confidence == Decimal("0.0")
    assert reason == "No crypto keyword match."


def test_crypto_v2_skips_without_link(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_crypto_snapshot(session, title="Will BTC exceed 70000?")

        assert CryptoV2Forecaster(settings=_settings()).forecast(session, snapshot) is None


def test_crypto_v2_skips_without_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_crypto_snapshot(session, title="Will BTC exceed 70000?")
        insert_crypto_market_link(
            session,
            ticker=snapshot.ticker,
            symbol="BTC",
            confidence="1.0",
            reason="test",
        )

        assert CryptoV2Forecaster(settings=_settings()).forecast(session, snapshot) is None


def test_crypto_v2_skips_without_minimum_feature_history(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_crypto_snapshot(session, title="Will BTC exceed 70000?")
        _seed_link_and_features(session, snapshot.ticker, momentum="0.5", history_minutes="59")

        assert CryptoV2Forecaster(settings=_settings()).forecast(session, snapshot) is None


def test_crypto_v2_adjusts_upward_for_positive_momentum_on_above_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_crypto_snapshot(session, title="Will BTC exceed 70000?")
        _seed_link_and_features(session, snapshot.ticker, momentum="0.5")

        forecast = CryptoV2Forecaster(settings=_settings()).forecast(session, snapshot)

        assert forecast is not None
        assert forecast.yes_probability == Decimal("0.49")
        assert forecast.feature_json["direction_detected"] == "ABOVE"


def test_crypto_v2_adjusts_downward_for_positive_momentum_on_below_market(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_crypto_snapshot(session, title="Will BTC be below 70000?")
        _seed_link_and_features(session, snapshot.ticker, momentum="0.5")

        forecast = CryptoV2Forecaster(settings=_settings()).forecast(session, snapshot)

        assert forecast is not None
        assert forecast.yes_probability == Decimal("0.41")
        assert forecast.feature_json["direction_detected"] == "BELOW"


def test_crypto_v2_scores_multi_component_crypto_link(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_crypto_snapshot(
            session,
            title="Will the crypto target-price basket finish in range?",
        )
        insert_crypto_market_link(
            session,
            ticker=snapshot.ticker,
            symbol="DOGE+ETH+XRP",
            confidence="0.72",
            reason="multi-asset target price component match",
            raw_json={
                "components": [
                    {"symbol": "DOGE", "direction": "ABOVE"},
                    {"symbol": "ETH", "direction": "BELOW"},
                    {"symbol": "XRP", "direction": "ABOVE"},
                ]
            },
        )
        _seed_crypto_features(session, "DOGE", momentum="0.3")
        _seed_crypto_features(session, "ETH", momentum="0.6")
        _seed_crypto_features(session, "XRP", momentum="0.9")

        forecast = CryptoV2Forecaster(settings=_settings()).forecast(session, snapshot)

    assert forecast is not None
    assert forecast.yes_probability == Decimal("0.4660")
    assert forecast.feature_json["direction_detected"] == "MULTI_COMPONENT"
    assert forecast.feature_json["component_symbols"] == ["DOGE", "ETH", "XRP"]
    assert forecast.feature_json["momentum_score"] == "0.2000"


def test_crypto_v2_skips_crypto_link_with_non_crypto_component_leg(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_crypto_snapshot(
            session,
            title="yes Target Price: $100,000,yes Over 5.5 runs scored",
        )
        _seed_link_and_features(session, snapshot.ticker, momentum="0.5")
        session.add(
            MarketLeg(
                ticker=snapshot.ticker,
                leg_index=0,
                parsed_at=utc_now(),
                side="YES",
                category="crypto",
                market_type="TARGET_PRICE",
                entity_name="BTC",
                operator="ABOVE",
                threshold_value="100000",
                unit="USD",
                confidence="0.95",
                raw_text="yes Target Price: $100,000",
                reason="test crypto leg",
                raw_json=encode_json({"source": "test"}),
            )
        )
        session.add(
            MarketLeg(
                ticker=snapshot.ticker,
                leg_index=1,
                parsed_at=utc_now(),
                side="YES",
                category="sports",
                market_type="TEAM_TOTAL",
                entity_name="Over 5.5 runs scored",
                operator="ABOVE",
                threshold_value="5.5",
                unit="runs",
                confidence="0.95",
                raw_text="yes Over 5.5 runs scored",
                reason="test sports leg",
                raw_json=encode_json({"source": "test"}),
            )
        )
        session.flush()

        forecast = CryptoV2Forecaster(settings=_settings()).forecast(session, snapshot)
        skip = session.scalar(
            select(ForecastSkipLog).where(ForecastSkipLog.ticker == snapshot.ticker)
        )

    assert forecast is None
    assert skip is not None
    assert "non-crypto component legs" in skip.reason


def test_crypto_backtest_handles_no_evaluated_trades(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = tmp_path / "crypto_backtest.md"
    with session_factory() as session:
        path = generate_crypto_backtest_report(session, days=30, output_path=output)

    text = path.read_text(encoding="utf-8")
    assert "Crypto Backtest" in text
    assert "crypto_v2" in text
    assert "0" in text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'crypto_phase27.db'}")
    return get_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        crypto_v2_max_adjustment=Decimal("0.08"),
        crypto_v2_min_link_confidence=Decimal("0.6"),
        crypto_v2_min_history_minutes=60,
    )


def _seed_crypto_snapshot(session, *, title: str):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": "BTC-MARKET",
            "status": "open",
            "title": title,
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.50", "10"]],
            }
        },
        now,
    )


def _seed_link_and_features(
    session,
    ticker: str,
    *,
    momentum: str,
    history_minutes: str = "60",
) -> None:
    insert_crypto_market_link(
        session,
        ticker=ticker,
        symbol="BTC",
        confidence="1.0",
        reason="test",
    )
    insert_crypto_features(
        session,
        symbol="BTC",
        source="test",
        generated_at=utc_now(),
        window_minutes=1440,
        features={
            "price": "100",
            "history_minutes": history_minutes,
            "momentum_score": momentum,
            "trend_direction": "UP",
        },
    )


def _seed_crypto_features(
    session,
    symbol: str,
    *,
    momentum: str,
    history_minutes: str = "60",
) -> None:
    insert_crypto_features(
        session,
        symbol=symbol,
        source="test",
        generated_at=utc_now(),
        window_minutes=1440,
        features={
            "price": "100",
            "history_minutes": history_minutes,
            "momentum_score": momentum,
            "trend_direction": "UP",
        },
    )
