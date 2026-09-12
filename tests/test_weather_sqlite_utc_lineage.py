from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import WeatherFeature, WeatherForecast
from kalshi_predictor.forecasting import weather_v2
from kalshi_predictor.weather.features import build_weather_features
from kalshi_predictor.weather.repository import (
    insert_weather_forecast_if_missing,
    weather_forecast_clock_consistent,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    WeatherForecast.__table__.create(engine)
    WeatherFeature.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_non_utc_insert_roundtrip_and_utc_dedupe(session):
    offset = timezone(timedelta(hours=-4))
    target = datetime(2026, 9, 10, 20, tzinfo=offset)
    generated = datetime(2026, 9, 10, 14, tzinfo=offset)
    values = dict(
        location_key="miami",
        source="noaa",
        forecast_time=target,
        forecast_generated_at=generated,
        temperature_f=85,
        raw_json={"startTime": target.isoformat()},
    )
    first, inserted = insert_weather_forecast_if_missing(session, **values)
    assert inserted
    first_id = first.id
    session.commit()
    session.expire_all()
    reread = session.get(WeatherForecast, first_id)
    assert reread.forecast_time == datetime(2026, 9, 11, 0)
    assert reread.forecast_generated_at == datetime(2026, 9, 10, 18)
    assert weather_forecast_clock_consistent(reread)
    second, inserted = insert_weather_forecast_if_missing(
        session,
        **{
            **values,
            "forecast_time": target.astimezone(UTC),
            "forecast_generated_at": generated.astimezone(UTC),
        },
    )
    assert not inserted
    assert second.id == first_id


def legacy_forecast(session):
    row = WeatherForecast(
        location_key="miami",
        source="noaa",
        forecast_time=datetime(2026, 9, 10, 20),
        forecast_generated_at=datetime(2026, 9, 10, 18),
        temperature_f="85",
        raw_json='{"startTime":"2026-09-10T20:00:00-04:00"}',
        created_at=datetime(2026, 9, 10, 18, 16),
    )
    session.add(row)
    session.flush()
    return row


def test_legacy_wrong_hour_cannot_build_fresh_feature(session):
    row = legacy_forecast(session)
    assert not weather_forecast_clock_consistent(row)
    result = build_weather_features(
        session,
        location_key="miami",
        limit=4,
        settings=SimpleNamespace(weather_v2_knyc_observation_enabled=False),
    )
    assert result.forecasts_processed == 1
    assert result.features_inserted == 0


def test_correct_utc_import_does_not_dedupe_against_legacy_wrong_hour(session):
    wrong = legacy_forecast(session)
    values = dict(
        location_key="miami",
        source="noaa",
        forecast_generated_at=datetime(2026, 9, 10, 18, tzinfo=UTC),
        forecast_time=datetime.fromisoformat("2026-09-10T16:00:00-04:00"),
        raw_json={"startTime": "2026-09-10T16:00:00-04:00"},
        temperature_f=88,
    )
    correct, inserted = insert_weather_forecast_if_missing(session, **values)
    assert inserted and correct.id != wrong.id
    session.commit()
    session.expire_all()
    repeated, inserted = insert_weather_forecast_if_missing(session, **values)
    assert not inserted and repeated.id == correct.id
    assert weather_forecast_clock_consistent(repeated)


@pytest.mark.parametrize("consistent_source_wrong_feature", [False, True])
def test_existing_legacy_feature_rejected_before_model_probability(
    session, monkeypatch, consistent_source_wrong_feature
):
    row = legacy_forecast(session)
    if consistent_source_wrong_feature:
        row.raw_json = '{"startTime":"2026-09-10T20:00:00Z"}'
        assert weather_forecast_clock_consistent(row)
    feature = SimpleNamespace(
        id=99,
        target_time=datetime(2026, 9, 10, 21, tzinfo=UTC),
        raw_json='{"source_observation_ref":'
        '{"table":"weather_forecasts","id":' + str(row.id) + "}}",
    )
    link = SimpleNamespace(confidence="1", location_key="miami")
    monkeypatch.setattr(weather_v2, "get_latest_weather_link_for_ticker", lambda *a, **kw: link)
    monkeypatch.setattr(
        weather_v2, "_features_for_link", lambda *a, **kw: (feature, "EXACT_TARGET_TIME")
    )
    skip = Mock()
    monkeypatch.setattr(weather_v2, "_skip", skip)
    settings = SimpleNamespace(
        weather_v2_min_link_confidence=Decimal("0.6"), weather_v2_default_location_key="miami"
    )
    snapshot = SimpleNamespace(captured_at=datetime.now(UTC) - timedelta(seconds=1), ticker="TEST")
    assert weather_v2.WeatherV2Forecaster(settings).forecast(session, snapshot) is None
    assert skip.call_args.args[2] == "weather source target timestamp mismatch"
