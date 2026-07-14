from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.forecasting import registry
from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.crypto.repository import insert_crypto_features, insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import Forecast, ForecastSkipLog
from kalshi_predictor.forecasting.registry import latest_snapshots_for_model, run_forecast_models
from kalshi_predictor.utils.time import utc_now


def test_latest_snapshots_for_model_uses_crypto_link_table(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        linked_snapshot = _seed_crypto_ready_snapshot(session)
        _seed_unlinked_newer_snapshot(session)

        rows = latest_snapshots_for_model(session, model_name="crypto_v2", limit=10)

    assert rows is not None
    assert [row.ticker for row in rows] == [linked_snapshot.ticker]


def test_forecast_cli_scopes_crypto_v2_to_crypto_linked_snapshots(
    tmp_path,
    monkeypatch,
) -> None:
    get_settings.cache_clear()
    db_url = f"sqlite:///{Path(tmp_path) / 'forecast_scoped_crypto.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("KALSHI_DB_URL", db_url)
    engine = init_db(db_url)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        crypto_snapshot = _seed_crypto_ready_snapshot(session)
        unrelated_snapshot = _seed_unlinked_newer_snapshot(session)
        session.commit()

    result = CliRunner().invoke(
        app,
        ["forecast", "--model", "crypto_v2", "--limit", "10"],
    )

    assert result.exit_code == 0
    assert "Scanned 1 snapshots" in result.output
    assert "Inserted 1 forecasts" in result.output
    with session_factory() as session:
        forecast = session.scalar(select(Forecast).where(Forecast.model_name == "crypto_v2"))
        unrelated_skip = session.scalar(
            select(ForecastSkipLog).where(
                ForecastSkipLog.model_name == "crypto_v2",
                ForecastSkipLog.ticker == unrelated_snapshot.ticker,
            )
        )

    get_settings.cache_clear()
    assert forecast is not None
    assert forecast.ticker == crypto_snapshot.ticker
    assert unrelated_skip is None


def test_run_forecast_models_reuses_forecaster_per_model(monkeypatch) -> None:
    calls: list[str] = []
    lifecycle: list[str] = []

    class FakeForecaster:
        def begin_forecast_run(self):
            lifecycle.append("begin")

        def end_forecast_run(self):
            lifecycle.append("end")

        def forecast(self, session, snapshot):
            del session, snapshot
            return None

    def fake_get_forecaster(name: str):
        calls.append(name)
        return FakeForecaster()

    monkeypatch.setattr(registry, "get_forecaster", fake_get_forecaster)
    monkeypatch.setattr(registry, "build_feature_snapshot", lambda session, snapshot: None)
    monkeypatch.setattr(registry, "capture_forecast_attempt", lambda *args, **kwargs: None)

    summary = run_forecast_models(
        object(),
        model_name="crypto_v2",
        snapshots=[SimpleNamespace(ticker="KXBTC-A"), SimpleNamespace(ticker="KXBTC-B")],
    )

    assert calls == ["crypto_v2"]
    assert lifecycle == ["begin", "end"]
    assert summary.snapshots_scanned == 2
    assert summary.forecasts_inserted == 0
    assert summary.skipped == 2


def test_run_forecast_models_ensures_signals_once_and_passes_snapshot(monkeypatch) -> None:
    forecasts: list[SimpleNamespace] = []
    ensured: list[object] = []
    attributed: list[dict[str, object]] = []

    class FakeForecaster:
        def forecast(self, session, snapshot):
            del session
            return SimpleNamespace(ticker=snapshot.ticker)

    snapshots = [SimpleNamespace(ticker="KXBTC-A"), SimpleNamespace(ticker="KXBTC-B")]

    monkeypatch.setattr(registry, "get_forecaster", lambda name: FakeForecaster())
    monkeypatch.setattr(registry, "build_feature_snapshot", lambda session, snapshot: None)
    monkeypatch.setattr(registry, "ensure_builtin_signals", lambda session: ensured.append(session))

    def fake_insert_forecast(session, forecast):
        del session
        record = SimpleNamespace(
            id=len(forecasts) + 1,
            ticker=forecast.ticker,
            model_name="crypto_v2",
        )
        forecasts.append(record)
        return record

    def fake_attribute_forecast_signals(session, record, **kwargs):
        attributed.append({"session": session, "record": record, **kwargs})
        return []

    monkeypatch.setattr(registry, "insert_forecast", fake_insert_forecast)
    monkeypatch.setattr(registry, "attribute_forecast_signals", fake_attribute_forecast_signals)

    session = object()
    summary = run_forecast_models(session, model_name="crypto_v2", snapshots=snapshots)

    assert summary.snapshots_scanned == 2
    assert summary.forecasts_inserted == 2
    assert summary.skipped == 0
    assert ensured == [session]
    assert [row["snapshot"] for row in attributed] == snapshots
    assert [row["ensure_builtin"] for row in attributed] == [False, False]


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'forecast_scoped.db'}")
    return get_session_factory(engine)


def _seed_crypto_ready_snapshot(session):
    now = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": "KXBTC-SCOPED",
            "status": "open",
            "title": "Will BTC exceed 70000?",
            "series_ticker": "KXBTC",
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
        generated_at=now - timedelta(minutes=5),
        window_minutes=1440,
        features={
            "price": "100",
            "history_minutes": "120",
            "momentum_score": "0.5",
            "trend_direction": "UP",
        },
    )
    return snapshot


def _seed_unlinked_newer_snapshot(session):
    return insert_market_snapshot(
        session,
        {
            "ticker": "KXSPORT-NEWER",
            "status": "open",
            "title": "Will an unrelated sports market resolve?",
            "yes_bid_dollars": "0.20",
            "yes_ask_dollars": "0.30",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.20", "10"]],
                "no_dollars": [["0.70", "10"]],
            }
        },
        utc_now() + timedelta(minutes=1),
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
        "event_ticker": "KXBTC-SCOPED",
        "market_type": "binary",
        "idempotency_key": "test-btc-terms",
    }
