from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.repository import insert_crypto_market_link
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import SignalSkipLog
from kalshi_predictor.economic.repository import insert_economic_market_link
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.signals.attribution import (
    attribute_forecast_signals,
    signal_badges_for_opportunity,
)
from kalshi_predictor.signals.registry import (
    ensure_builtin_signals,
    expected_signal_names,
)
from kalshi_predictor.signals.signal_types import (
    BREAKING_NEWS_SIGNAL,
    CRYPTO_SIGNAL,
    ECONOMIC_SIGNAL,
)
from kalshi_predictor.signals.skip_log import log_signal_skip
from kalshi_predictor.signals.status import signal_status_rows
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_all_expected_signals_are_registered(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        signals = ensure_builtin_signals(session)

    registered = {signal.signal_name for signal in signals}
    assert set(expected_signal_names()).issubset(registered)


def test_crypto_signal_skips_with_no_crypto_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(
            session,
            ticker="SIG-BTC",
            title="Will Bitcoin be above 100000 tomorrow?",
        )
        insert_crypto_market_link(
            session,
            ticker=snapshot.ticker,
            symbol="BTC",
            confidence=Decimal("1.0"),
            reason="test link",
        )
        forecast = _seed_forecast(session, ticker=snapshot.ticker, model_name="crypto_v2")

        attribute_forecast_signals(session, forecast)
        skip = session.scalar(
            select(SignalSkipLog).where(SignalSkipLog.signal_name == CRYPTO_SIGNAL)
        )

    assert skip is not None
    assert skip.reason == "no crypto features"


def test_economic_signal_skips_with_no_economic_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(
            session,
            ticker="SIG-CPI",
            title="Will CPI exceed 3 percent this month?",
        )
        insert_economic_market_link(
            session,
            ticker=snapshot.ticker,
            event_key="cpi",
            category="inflation",
            confidence=Decimal("0.9"),
            reason="test link",
        )
        forecast = _seed_forecast(session, ticker=snapshot.ticker, model_name="economic_v1")

        attribute_forecast_signals(session, forecast)
        skip = session.scalar(
            select(SignalSkipLog).where(SignalSkipLog.signal_name == ECONOMIC_SIGNAL)
        )

    assert skip is not None
    assert skip.reason == "no economic features"


def test_news_signal_marks_needs_data_when_news_is_empty(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        rows = signal_status_rows(session)

    row = next(item for item in rows if item["signal_name"] == BREAKING_NEWS_SIGNAL)
    assert row["readiness_status"] == "NEEDS_DATA"
    assert row["missing_data"] == "news_items"
    assert "News ingestion not connected yet." in row["next_action"]


def test_signal_skip_log_writes_correctly(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        row = log_signal_skip(
            session,
            signal_name=CRYPTO_SIGNAL,
            ticker="SKIP-SIGNAL",
            reason="no crypto features",
            required_data=["crypto_features"],
            available_data={"crypto_link": True},
        )
        loaded = session.scalar(select(SignalSkipLog).where(SignalSkipLog.id == row.id))

    assert loaded is not None
    assert loaded.reason == "no crypto features"


def test_signal_badges_do_not_write_skip_logs(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot(
            session,
            ticker="SIG-UI-BTC",
            title="Will Bitcoin be above 100000 tomorrow?",
        )
        insert_crypto_market_link(
            session,
            ticker=snapshot.ticker,
            symbol="BTC",
            confidence=Decimal("1.0"),
            reason="test link",
        )

        badges = signal_badges_for_opportunity(
            session,
            ticker=snapshot.ticker,
            model_name="crypto_v2",
        )
        skip_count = session.scalar(select(func.count()).select_from(SignalSkipLog))

    assert isinstance(badges, list)
    assert skip_count == 0


def test_signals_status_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'signals_status.db'}"

    result = runner.invoke(app, ["signals-status"], env={"KALSHI_DB_URL": db_url})

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert "Signals status" in result.output
    assert "signal=crypto" in result.output
    assert "missing=" in result.output


def test_signals_report_cli_smoke(tmp_path) -> None:
    get_settings.cache_clear()
    runner = CliRunner()
    db_url = f"sqlite:///{Path(tmp_path) / 'signals_report.db'}"
    output = Path(tmp_path) / "signals_report.md"

    result = runner.invoke(
        app,
        ["signals-report", "--output", str(output)],
        env={"KALSHI_DB_URL": db_url},
    )

    get_settings.cache_clear()
    assert result.exit_code == 0
    assert output.exists()
    assert "Inactive Signals" in output.read_text(encoding="utf-8")


def test_signal_leaderboard_labels_inactive_signals_in_ui(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    client = TestClient(
        create_app(
            session_factory=session_factory,
            settings=Settings(overnight_require_market_data=False),
        )
    )

    marketplace = client.get("/signals")
    health = client.get("/signals/health")

    assert marketplace.status_code == 200
    assert "Missing Data" in marketplace.text
    assert "Needs data" in marketplace.text
    assert "News ingestion not connected yet." in marketplace.text
    assert health.status_code == 200
    assert "Signal Health" in health.text
    assert "Inactive Signals" in health.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'signal_readiness.db'}")
    return get_session_factory(engine)


def _seed_snapshot(session, *, ticker: str, title: str):
    now = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": title,
            "close_time": (now + timedelta(hours=4)).isoformat(),
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.55",
            "last_price_dollars": "0.50",
        },
        {"orderbook": {"yes": [[45, 10]], "no": [[45, 10]]}},
        now,
    )
    session.flush()
    return snapshot


def _seed_forecast(session, *, ticker: str, model_name: str):
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=utc_now(),
            model_name=model_name,
            yes_probability=Decimal("0.60"),
            market_mid_probability=Decimal("0.50"),
            best_yes_bid=Decimal("0.45"),
            best_yes_ask=Decimal("0.55"),
            feature_json={},
        ),
    )
    session.flush()
    return forecast
