from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import EconomicFeature, EconomicMarketLink
from kalshi_predictor.economic.repository import (
    get_latest_economic_feature,
    get_latest_economic_link_for_ticker,
)
from kalshi_predictor.forecasting import economic_v1

AT = datetime(2026, 9, 8, 13, tzinfo=UTC)


@pytest.fixture
def session(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'economic-clocks.db'}")
    with get_session_factory(engine)() as value:
        yield value
    engine.dispose()


def seed(session):
    snapshot = insert_market_snapshot(
        session,
        {"ticker": "KXCPI-TEST", "title": "CPI above 0.2%?", "status": "open",
         "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.50"},
        {}, AT - timedelta(seconds=10),
    )
    link = EconomicMarketLink(
        ticker=snapshot.ticker, event_key="cpi_test", detected_at=AT,
        category="inflation", confidence="1", reason="fixture", raw_json="{}",
    )
    feature = EconomicFeature(
        event_key="cpi_test", generated_at=AT, created_at=AT,
        category="inflation", surprise_score="0.1", direction="UP",
        confidence_score="70", raw_json="{}",
    )
    session.add_all([link, feature])
    session.flush()
    return snapshot, link, feature


def test_actual_computation_clock_and_exact_cutoff_inputs(session, monkeypatch):
    snapshot, link, feature = seed(session)
    clocks = iter((AT, AT + timedelta(seconds=1)))
    monkeypatch.setattr(economic_v1, "utc_now", lambda: next(clocks))
    forecast = economic_v1.EconomicV1Forecaster().forecast(session, snapshot)
    assert forecast is not None
    assert forecast.forecasted_at == AT + timedelta(seconds=1)
    assert forecast.yes_probability == Decimal("0.457")
    assert forecast.feature_json["input_cutoff"] == AT.isoformat()
    assert forecast.feature_json["snapshot_captured_at"] == snapshot.captured_at.isoformat()
    assert forecast.feature_json["feature_created_at"] == AT.isoformat()
    assert forecast.feature_json["economic_feature_id"] == feature.id
    assert forecast.feature_json["link_detected_at"] == link.detected_at.isoformat()


@pytest.mark.parametrize("clock", ["generated_at", "created_at"])
def test_future_or_backdated_later_inserted_feature_is_excluded(session, monkeypatch, clock):
    snapshot, _, feature = seed(session)
    setattr(feature, clock, AT + timedelta(microseconds=1))
    session.flush()
    monkeypatch.setattr(economic_v1, "utc_now", lambda: AT)
    assert economic_v1.EconomicV1Forecaster().forecast(session, snapshot) is None


def test_future_link_is_excluded(session, monkeypatch):
    snapshot, link, _ = seed(session)
    link.detected_at = AT + timedelta(microseconds=1)
    session.flush()
    monkeypatch.setattr(economic_v1, "utc_now", lambda: AT)
    assert economic_v1.EconomicV1Forecaster().forecast(session, snapshot) is None


def test_future_snapshot_is_refused(session, monkeypatch):
    snapshot, _, _ = seed(session)
    snapshot.captured_at = AT + timedelta(seconds=1)
    monkeypatch.setattr(economic_v1, "utc_now", lambda: AT)
    assert economic_v1.EconomicV1Forecaster().forecast(session, snapshot) is None


def test_backward_computation_clock_is_refused(session, monkeypatch):
    snapshot, _, _ = seed(session)
    clocks = iter((AT, AT - timedelta(seconds=1)))
    monkeypatch.setattr(economic_v1, "utc_now", lambda: next(clocks))
    assert economic_v1.EconomicV1Forecaster().forecast(session, snapshot) is None


def test_selection_preserves_nonnull_preference_and_id_tiebreak(session):
    snapshot, _, original = seed(session)
    original.generated_at = original.created_at = AT - timedelta(seconds=1)
    _, later_link, later = seed(session)
    later.surprise_score = None
    session.flush()
    assert get_latest_economic_feature(session, "cpi_test", as_of=AT).id == original.id
    later.surprise_score = "0.2"
    later.generated_at = original.generated_at
    session.flush()
    assert get_latest_economic_feature(session, "cpi_test", as_of=AT).id == later.id
    selected_link = get_latest_economic_link_for_ticker(session, snapshot.ticker, as_of=AT)
    assert selected_link.id == later_link.id


def test_ineligible_newer_rows_do_not_hide_available_inputs(session, monkeypatch):
    snapshot, _, original = seed(session)
    _, future_link, future_feature = seed(session)
    future_link.detected_at = AT + timedelta(seconds=1)
    future_link.event_key = "different_event"
    future_feature.generated_at = AT + timedelta(seconds=1)
    future_feature.surprise_score = "1"
    session.flush()
    monkeypatch.setattr(economic_v1, "utc_now", lambda: AT)
    forecast = economic_v1.EconomicV1Forecaster().forecast(session, snapshot)
    assert forecast is not None
    assert forecast.feature_json["economic_feature_id"] == original.id
    assert forecast.yes_probability == Decimal("0.457")


def test_naive_caller_cutoff_is_refused(session):
    with pytest.raises(ValueError, match="TIMEZONE_REQUIRED"):
        get_latest_economic_feature(session, "cpi_test", as_of=AT.replace(tzinfo=None))
    with pytest.raises(ValueError, match="TIMEZONE_REQUIRED"):
        get_latest_economic_link_for_ticker(session, "KXCPI-TEST", as_of=AT.replace(tzinfo=None))
