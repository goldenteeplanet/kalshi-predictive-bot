import json
from datetime import timedelta
from pathlib import Path

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import Market
from kalshi_predictor.phase_prov14b_r1 import (
    diagnose_weather_snapshot_eligibility,
    write_prov14b_r1_repair_preview,
)
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.repository import insert_weather_market_link


def _link(session, ticker, *, target_time):
    insert_weather_market_link(
        session,
        ticker=ticker,
        location_key="new_york",
        weather_metric="temperature_high",
        target_operator="ABOVE",
        target_value="80",
        target_time=target_time,
        confidence="1",
        reason="exact test",
    )


def _market(session, ticker, *, close_time, status="open", snapshot_status="open"):
    now = utc_now()
    upsert_market(session, {
        "ticker": ticker,
        "status": status,
        "close_time": close_time.isoformat() if close_time else None,
        "title": "NYC exact eligibility test",
    })
    insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": snapshot_status,
            "close_time": close_time.isoformat() if close_time else None,
        },
        {"orderbook_fp": {}},
        now,
    )
    # Snapshot ingestion mirrors its status onto markets; restore the intended
    # independent market status so each gate is isolated by this fixture.
    session.get(Market, ticker).status = status


def test_diagnostic_attributes_every_exact_exclusion(tmp_path):
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'r1.db'}"))
    now = utc_now()
    with factory() as session:
        _market(session, "WX-OK", close_time=now + timedelta(hours=2))
        _link(session, "WX-OK", target_time=now + timedelta(hours=1))
        _market(session, "WX-CLOSED", close_time=now - timedelta(minutes=1))
        _link(session, "WX-CLOSED", target_time=now)
        _market(session, "WX-MKT-STATUS", close_time=now + timedelta(hours=2), status="closed")
        _link(session, "WX-MKT-STATUS", target_time=now + timedelta(hours=1))
        _market(
            session,
            "WX-SNAP-STATUS",
            close_time=now + timedelta(hours=2),
            snapshot_status="settled",
        )
        _link(session, "WX-SNAP-STATUS", target_time=now + timedelta(hours=1))
        _market(session, "WX-NO-CLOSE", close_time=None)
        _link(session, "WX-NO-CLOSE", target_time=now + timedelta(hours=1))
        _link(session, "WX-NO-MARKET", target_time=now + timedelta(hours=1))
        _market(session, "WX-NO-TARGET", close_time=now + timedelta(hours=2))
        _link(session, "WX-NO-TARGET", target_time=None)
        session.commit()
        result = diagnose_weather_snapshot_eligibility(session, as_of=now)

    rows = {row["ticker"]: row for row in result["rows"]}
    assert result["eligible_ticker_count"] == 1, rows
    assert rows["WX-OK"]["eligible"] is True
    assert rows["WX-CLOSED"]["exclusion_reasons"] == ["MARKET_NOT_FUTURE"]
    assert rows["WX-MKT-STATUS"]["exclusion_reasons"] == ["INACTIVE_MARKET_STATUS"]
    assert rows["WX-SNAP-STATUS"]["exclusion_reasons"] == ["INACTIVE_SNAPSHOT_STATUS"]
    assert rows["WX-NO-CLOSE"]["exclusion_reasons"] == ["MISSING_MARKET_CLOSE_TIME"]
    assert rows["WX-NO-MARKET"]["exclusion_reasons"] == ["MISSING_MARKET", "MISSING_SNAPSHOT"]
    assert rows["WX-NO-TARGET"]["exclusion_reasons"] == ["MISSING_LINK_TARGET_TIME"]


def test_preview_is_deterministic_and_strictly_no_write(tmp_path):
    first = json.loads(write_prov14b_r1_repair_preview(tmp_path / "one").read_text())
    second = json.loads(write_prov14b_r1_repair_preview(tmp_path / "two").read_text())
    assert first == second
    assert first["database_access"] is False
    assert first["database_writes"] == 0
    assert first["thresholds_changed"] is False
    assert first["fuzzy_matching_used"] is False
    assert first["guarded_cloud_retry_requires_new_approval"] is True


def test_bounded_cycle_pins_weather_before_feature_write_and_exactly_aligns_target():
    source = (Path(__file__).parents[1] / "scripts" / "prov14_bounded_cycle.py").read_text()
    pin = source.index('weather_snapshots = latest_snapshots_for_model(')
    fail_closed = source.index(
        'raise RuntimeError("No exact current weather snapshots are eligible")'
    )
    first_weather_write = source.index("insert_weather_features(")
    assert pin < fail_closed < first_weather_write
    assert "WeatherForecast.location_key == link.location_key" in source
    assert "WeatherForecast.forecast_time == link.target_time" in source
    assert "latest stored" not in source.lower()
