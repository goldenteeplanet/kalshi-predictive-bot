import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.crypto.features import build_crypto_features
from kalshi_predictor.crypto.repository import insert_crypto_price
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast
from kalshi_predictor.data.schema import (
    CryptoFeature,
    Market,
    MarketSnapshot,
    RuntimeProvenanceEvent,
    WeatherForecast,
)
from kalshi_predictor.forecasting.crypto_v2 import _primary_observation_reference
from kalshi_predictor.forecasting.weather_v2 import _feature_source_reference
from kalshi_predictor.phase_prov13 import build_prov13_repair_preview
from kalshi_predictor.weather.features import build_weather_features
from kalshi_predictor.weather.repository import insert_weather_forecast


def test_prov13_future_crypto_features_carry_exact_source_reference(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = datetime(2026, 7, 17, 6, 0, tzinfo=UTC)
    with session_factory() as session:
        price = insert_crypto_price(
            session, symbol="BTC", source="coinbase", observed_at=now,
            price_usd="60000", raw_json={"exact": True},
        )
        build_crypto_features(session, symbols=["BTC"], source="coinbase")
        feature = session.query(CryptoFeature).one()
        raw = json.loads(feature.raw_json)
        reference = _primary_observation_reference([{"features": feature}])

    assert raw["source_observation_ref"]["id"] == price.id
    assert reference == raw["source_observation_ref"]
    assert reference["table"] == "crypto_prices"


def test_prov13_future_weather_features_carry_exact_source_reference(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = datetime(2026, 7, 17, 6, 0, tzinfo=UTC)
    with session_factory() as session:
        source = insert_weather_forecast(
            session, location_key="new_york", source="noaa",
            forecast_generated_at=now, forecast_time=now,
            temperature_f="80", raw_json={"exact": True},
        )
        build_weather_features(session, location_key="new_york")
        feature = session.query(CryptoFeature).first()
        assert feature is None
        from kalshi_predictor.data.schema import WeatherFeature
        weather_feature = session.query(WeatherFeature).one()
        reference = _feature_source_reference(weather_feature)

    assert reference["table"] == "weather_forecasts"
    assert reference["id"] == source.id
    assert reference["forecast_time"] == now.isoformat()


def test_prov13_historical_preview_is_exact_and_does_not_mutate(tmp_path, monkeypatch) -> None:
    _disable_memory_capture(monkeypatch)
    session_factory = _session_factory(tmp_path)
    now = datetime(2026, 7, 17, 6, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(Market(
            ticker="PROV13", raw_json="{}", first_seen_at=now, last_seen_at=now
        ))
        price = insert_crypto_price(
            session, symbol="BTC", source="coinbase", observed_at=now,
            price_usd="60000",
        )
        feature = CryptoFeature(
            symbol="BTC", source="coinbase", generated_at=now, window_minutes=60,
            price="60000", trend_direction="flat",
            raw_json=json.dumps({
                "source_latest_observed_at": now.isoformat(),
                "price_source": "coinbase",
            }), created_at=now,
        )
        snapshot = MarketSnapshot(
            ticker="PROV13", captured_at=now, raw_market_json="{}"
        )
        session.add_all([feature, snapshot])
        session.flush()
        insert_forecast(session, {
            "ticker": "PROV13", "forecasted_at": now,
            "model_name": "crypto_v2", "yes_probability": Decimal("0.60"),
            "feature_json": {"crypto_feature_id": feature.id},
        }, market_snapshot_id=None, attribution_enabled=True)
        session.commit()
        event = session.query(RuntimeProvenanceEvent).one()
        before = (event.source_observation_ref_json, event.market_snapshot_id, event.raw_json)
        preview = build_prov13_repair_preview(session, limit=10)
        session.refresh(event)
        after = (event.source_observation_ref_json, event.market_snapshot_id, event.raw_json)

    row = preview["rows"][0]
    assert row["status"] == "SAFE_EXACT_PREVIEW"
    assert row["proposed_source_observation_ref"]["id"] == price.id
    assert row["proposed_market_snapshot_id"] == snapshot.id
    assert before == after
    assert preview["database_writes"] == 0
    assert preview["existing_provenance_rows_modified"] == 0


def _session_factory(tmp_path: Path):
    engine = init_db(f"sqlite:///{tmp_path / 'prov13.db'}")
    return get_session_factory(engine)


def _disable_memory_capture(monkeypatch) -> None:
    import kalshi_predictor.memory.capture as capture
    monkeypatch.setattr(capture, "capture_forecast_created", lambda *args, **kwargs: None)
