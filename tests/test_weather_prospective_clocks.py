import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import WeatherFeature, WeatherMarketLink
from kalshi_predictor.forecasting import weather_v2
from kalshi_predictor.weather.repository import (
    get_latest_weather_features,
    get_latest_weather_link_for_ticker,
)

AT = datetime(2026, 9, 9, 7, tzinfo=UTC)


@pytest.fixture
def session(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'weather-clocks.db'}")
    with get_session_factory(engine)() as value:
        yield value
    engine.dispose()


def seed(session):
    target = AT + timedelta(hours=1)
    snapshot = insert_market_snapshot(
        session,
        {"ticker": "WX-CLOCK", "title": "Temperature above 70?", "status": "open",
         "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.50"},
        {}, AT - timedelta(seconds=10),
    )
    link = WeatherMarketLink(
        ticker=snapshot.ticker, location_key="new_york", detected_at=AT,
        weather_metric="TEMPERATURE", target_operator="ABOVE", target_value="70",
        target_time=target, confidence="1", reason="fixture", raw_json="{}",
    )
    feature = WeatherFeature(
        location_key="new_york", source="test", generated_at=AT, created_at=AT,
        target_time=target, temperature_f="80",
        raw_json=json.dumps({"forecast_generated_at": (AT - timedelta(hours=1)).isoformat(),
                             "forecast_age_hours": "1"}),
    )
    session.add_all([link, feature])
    session.flush()
    return snapshot, link, feature


def model():
    return weather_v2.WeatherV2Forecaster(Settings(_env_file=None))


def test_actual_generation_time_and_input_cutoff_are_distinct(session, monkeypatch):
    snapshot, link, feature = seed(session)
    clocks = iter((AT, AT + timedelta(seconds=1)))
    monkeypatch.setattr(weather_v2, "utc_now", lambda: next(clocks))
    result = model().forecast(session, snapshot)
    assert result is not None
    assert result.forecasted_at == AT + timedelta(seconds=1)
    assert result.yes_probability == Decimal("0.50")
    assert result.feature_json["input_cutoff"] == AT.isoformat()
    assert result.feature_json["snapshot_captured_at"] == snapshot.captured_at.isoformat()
    assert result.feature_json["feature_created_at"] == feature.created_at.isoformat()
    assert result.feature_json["link_detected_at"] == link.detected_at.isoformat()


@pytest.mark.parametrize("clock", ["generated_at", "created_at"])
def test_future_feature_is_excluded(session, monkeypatch, clock):
    snapshot, _, feature = seed(session)
    setattr(feature, clock, AT + timedelta(microseconds=1))
    session.flush()
    monkeypatch.setattr(weather_v2, "utc_now", lambda: AT)
    assert model().forecast(session, snapshot) is None


def test_future_link_is_excluded(session, monkeypatch):
    snapshot, link, _ = seed(session)
    link.detected_at = AT + timedelta(microseconds=1)
    session.flush()
    monkeypatch.setattr(weather_v2, "utc_now", lambda: AT)
    assert model().forecast(session, snapshot) is None


def test_future_snapshot_is_excluded(session, monkeypatch):
    snapshot, _, _ = seed(session)
    snapshot.captured_at = AT + timedelta(microseconds=1)
    monkeypatch.setattr(weather_v2, "utc_now", lambda: AT)
    assert model().forecast(session, snapshot) is None


def test_backward_generation_clock_is_excluded(session, monkeypatch):
    snapshot, _, _ = seed(session)
    clocks = iter((AT, AT - timedelta(seconds=1)))
    monkeypatch.setattr(weather_v2, "utc_now", lambda: next(clocks))
    assert model().forecast(session, snapshot) is None


@pytest.mark.parametrize("source_time", [AT + timedelta(seconds=1), AT - timedelta(hours=25)])
def test_cached_age_cannot_hide_future_or_stale_source(session, monkeypatch, source_time):
    snapshot, _, feature = seed(session)
    feature.raw_json = json.dumps({"forecast_generated_at": source_time.isoformat(),
                                  "forecast_age_hours": "1"})
    monkeypatch.setattr(weather_v2, "utc_now", lambda: AT)
    assert model().forecast(session, snapshot) is None


def test_cached_age_advances_when_no_source_clock_is_available(session, monkeypatch):
    snapshot, _, feature = seed(session)
    feature.generated_at = feature.created_at = AT - timedelta(hours=25)
    feature.raw_json = '{"forecast_age_hours":"1"}'
    session.flush()
    monkeypatch.setattr(weather_v2, "utc_now", lambda: AT)
    assert model().forecast(session, snapshot) is None


def test_future_reference_clock_is_not_hidden_by_feature_age(session, monkeypatch):
    snapshot, _, feature = seed(session)
    raw = json.loads(feature.raw_json)
    raw["source_observation_ref"] = {"available_at": (AT + timedelta(seconds=1)).isoformat()}
    feature.raw_json = json.dumps(raw)
    monkeypatch.setattr(weather_v2, "utc_now", lambda: AT)
    assert model().forecast(session, snapshot) is None


def test_refresh_eligibility_rejects_future_source_clock(session, monkeypatch):
    from kalshi_predictor import phase_gh1p

    snapshot, _, feature = seed(session)
    feature.raw_json = json.dumps(
        {"forecast_generated_at": (AT + timedelta(seconds=1)).isoformat()}
    )
    monkeypatch.setattr(phase_gh1p, "utc_now", lambda: AT)
    assert phase_gh1p._candidate_eligibility(
        session, "weather_v2", {"ticker": snapshot.ticker}, settings=Settings(_env_file=None)
    ) == (False, "WEATHER_SOURCE_IN_FUTURE")


def test_available_rows_are_not_hidden_by_future_rows(session, monkeypatch):
    snapshot, _, original = seed(session)
    original.generated_at = original.created_at = AT - timedelta(seconds=1)
    _, future_link, future_feature = seed(session)
    future_link.detected_at = future_feature.created_at = AT + timedelta(seconds=1)
    future_link.location_key = "austin"
    future_feature.temperature_f = "0"
    session.flush()
    monkeypatch.setattr(weather_v2, "utc_now", lambda: AT)
    result = model().forecast(session, snapshot)
    assert result is not None
    assert result.feature_json["weather_feature_id"] == original.id
    assert result.yes_probability == Decimal("0.50")


def test_naive_cutoff_is_refused(session):
    with pytest.raises(ValueError, match="TIMEZONE_REQUIRED"):
        get_latest_weather_features(session, "new_york", as_of=AT.replace(tzinfo=None))
    with pytest.raises(ValueError, match="TIMEZONE_REQUIRED"):
        get_latest_weather_link_for_ticker(session, "WX-CLOCK", as_of=AT.replace(tzinfo=None))


@pytest.mark.parametrize(
    "generated_at,available",
    [(None, False), ((AT + timedelta(seconds=1)).isoformat(), False), (AT.isoformat(), True)],
)
def test_monthly_calibration_requires_available_generation_clock(
    tmp_path, monkeypatch, generated_at, available
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "reports/phase_gh2/cliaus_monthly_rain_prepare.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"activation_permitted": True, "generated_at": generated_at,
                               "calibration": {"passed": True, "sample_count": 12,
                                               "residual_sigma_inches": "1"}}))
    link = WeatherMarketLink(location_key="austin", target_value="1")
    result = weather_v2._calibrated_monthly_rain_probability(link, as_of=AT)
    assert (result is not None) == available
    if result is not None:
        assert result[1]["local_generation_clock_checked"] is True
        assert "no_leakage" not in result[1]
