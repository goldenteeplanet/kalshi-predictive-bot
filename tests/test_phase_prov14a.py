import json
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.forecasting import registry
from kalshi_predictor.forecasting.registry import latest_snapshots_for_model, run_forecast_models
from kalshi_predictor.phase_prov14a import write_prov14a_repair_preview
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.repository import insert_weather_market_link


def test_prov14a_weather_selector_excludes_closed_and_stale_status_rows(tmp_path):
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'prov14a.db'}"))
    now = utc_now()
    with factory() as session:
        _seed_weather(session, "WX-CURRENT", now + timedelta(hours=2), "open", "open")
        _seed_weather(session, "WX-CLOSED", now - timedelta(hours=1), "open", "open")
        _seed_weather(session, "WX-STALE-SNAPSHOT", now + timedelta(hours=2), "open", "closed")
        session.commit()
        rows = latest_snapshots_for_model(
            session, model_name="weather_v2", limit=10, as_of=now
        )
    assert [row.ticker for row in rows] == ["WX-CURRENT"]


def test_prov14a_preview_is_deterministic_and_no_write(tmp_path):
    first = json.loads(write_prov14a_repair_preview(tmp_path / "a").read_text())
    second = json.loads(write_prov14a_repair_preview(tmp_path / "b").read_text())
    assert first == second
    assert first["database_access"] is False
    assert first["database_writes"] == 0
    assert first["cloud_runtime_modified"] is False
    assert first["guarded_cloud_retry_requires_new_approval"] is True


def test_prov14a_registry_passes_exact_snapshot_id_to_forecast_writer(monkeypatch):
    passed = []
    snapshot = SimpleNamespace(id=417, ticker="KXBTC-EXACT")

    class Forecaster:
        def forecast(self, session, selected):
            return SimpleNamespace(ticker=selected.ticker)

    monkeypatch.setattr(registry, "get_forecaster", lambda name: Forecaster())
    monkeypatch.setattr(registry, "build_feature_snapshot", lambda *args: None)
    monkeypatch.setattr(registry, "ensure_builtin_signals", lambda *args: None)
    monkeypatch.setattr(registry, "attribute_forecast_signals", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        registry,
        "insert_forecast",
        lambda session, forecast, *, market_snapshot_id: (
            passed.append(market_snapshot_id) or SimpleNamespace(id=1, ticker=forecast.ticker)
        ),
    )
    summary = run_forecast_models(object(), model_name="crypto_v2", snapshots=[snapshot])
    assert summary.forecasts_inserted == 1
    assert passed == [417]


def _seed_weather(session, ticker, close_time, market_status, snapshot_status):
    now = utc_now()
    upsert_market(session, {
        "ticker": ticker,
        "status": market_status,
        "close_time": close_time.isoformat(),
        "title": "NYC temperature test",
    })
    insert_weather_market_link(
        session,
        ticker=ticker,
        location_key="new_york",
        weather_metric="temperature_high",
        target_operator="ABOVE",
        target_value="80",
        target_time=close_time,
        confidence="1",
        reason="exact test",
    )
    return insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": snapshot_status,
            "close_time": close_time.isoformat(),
            "yes_bid_dollars": "0.4",
            "yes_ask_dollars": "0.5",
        },
        {"orderbook_fp": {}},
        now,
    )
