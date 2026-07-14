from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_settlement,
)
from kalshi_predictor.data.schema import Signal, SignalForecast, SignalTrade
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.paper.ledger import create_paper_order
from kalshi_predictor.paper.models import BUY_YES, PaperDecision
from kalshi_predictor.research.evidence import build_opportunity_evidence
from kalshi_predictor.signals.attribution import attribute_forecast_signals
from kalshi_predictor.signals.registry import ensure_builtin_signals
from kalshi_predictor.signals.reports import generate_signal_report
from kalshi_predictor.signals.repository import signal_explorer_rows, signal_leaderboard_rows
from kalshi_predictor.signals.scoring import refresh_signal_performance
from kalshi_predictor.signals.signal_types import MARKET_DIVERGENCE_SIGNAL, NEWS_SIGNAL
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_signal_creation_registers_builtin_signals(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        signals = ensure_builtin_signals(session)
        session.commit()
        count = session.scalar(select(func.count(Signal.id)))

    assert len(signals) >= 16
    assert count and count >= 16
    assert any(signal.signal_name == MARKET_DIVERGENCE_SIGNAL for signal in signals)
    assert any(signal.signal_name == NEWS_SIGNAL for signal in signals)


def test_signal_attribution_links_forecasts_and_paper_trades(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        forecast = _seed_signal_market(session)
        forecast_links = attribute_forecast_signals(session, forecast)
        order = create_paper_order(session, _paper_decision(forecast.id))
        session.commit()
        forecast_count = session.scalar(select(func.count(SignalForecast.id)))
        trade_count = session.scalar(select(func.count(SignalTrade.id)))

    assert any(link.signal_name == MARKET_DIVERGENCE_SIGNAL for link in forecast_links)
    assert order is not None
    assert forecast_count and forecast_count > 0
    assert trade_count and trade_count > 0


def test_signal_roi_calculation_and_leaderboard_ranking(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        forecast = _seed_signal_market(session)
        attribute_forecast_signals(session, forecast)
        create_paper_order(session, _paper_decision(forecast.id))
        upsert_settlement(session, {"ticker": "SIGNAL-GOOD", "result": "yes"})
        rows = refresh_signal_performance(session)
        leaderboard = signal_leaderboard_rows(session)

    divergence = next(row for row in rows if row.signal_name == MARKET_DIVERGENCE_SIGNAL)
    assert Decimal(divergence.total_pnl or "0") > 0
    assert Decimal(divergence.roi or "0") > 0
    assert any(row["signal_name"] == MARKET_DIVERGENCE_SIGNAL for row in leaderboard)


def test_signal_explorer_and_report_generation(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output = Path(tmp_path) / "signal_report.md"
    with session_factory() as session:
        forecast = _seed_signal_market(session)
        attribute_forecast_signals(session, forecast)
        create_paper_order(session, _paper_decision(forecast.id))
        upsert_settlement(session, {"ticker": "SIGNAL-GOOD", "result": "yes"})
        rows = signal_explorer_rows(session, refresh=True)
        report = generate_signal_report(session, output_path=output)
        session.commit()

    assert any(row["signal_name"] == MARKET_DIVERGENCE_SIGNAL for row in rows)
    assert report.exists()
    assert "Signal Marketplace Report" in report.read_text(encoding="utf-8")


def test_signal_ui_pages_and_research_integration(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        forecast = _seed_signal_market(session)
        attribute_forecast_signals(session, forecast)
        create_paper_order(session, _paper_decision(forecast.id))
        upsert_settlement(session, {"ticker": "SIGNAL-GOOD", "result": "yes"})
        refresh_signal_performance(session)
        evidence = build_opportunity_evidence(
            session,
            ticker="SIGNAL-GOOD",
            model_name="ensemble_v2",
        )
        session.commit()

    client = TestClient(
        create_app(
            session_factory=session_factory,
            settings=Settings(overnight_require_market_data=False),
        )
    )
    marketplace = client.get("/signals")
    detail = client.get("/signals/Market%20Divergence%20Signal")
    dashboard = client.get("/")

    assert str(evidence["primary_signal"]).endswith("Signal")
    assert evidence["signal_badges"]
    assert marketplace.status_code == 200
    assert "Signal Marketplace" in marketplace.text
    assert detail.status_code == 200
    assert "Market Divergence Signal" in detail.text
    assert dashboard.status_code == 200
    assert "/signals/" in dashboard.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3g.db'}")
    return get_session_factory(engine)


def _seed_signal_market(session):
    now = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": "SIGNAL-GOOD",
            "status": "open",
            "title": "Will Bitcoin be above 100k by July 31?",
            "series_ticker": "KXCRYPTO",
            "event_ticker": "KXCRYPTO-EVENT",
            "close_time": (now + timedelta(hours=5)).isoformat(),
            "yes_ask_dollars": "0.48",
            "liquidity_dollars": "12000",
            "volume_fp": "1000",
            "open_interest_fp": "500",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.46", "20"]],
                "no_dollars": [["0.50", "20"]],
            }
        },
        now,
    )
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker="SIGNAL-GOOD",
            forecasted_at=now,
            model_name="ensemble_v2",
            yes_probability=Decimal("0.66"),
            market_mid_probability=Decimal("0.48"),
            best_yes_bid=Decimal("0.46"),
            best_yes_ask=Decimal(snapshot.best_yes_ask),
            feature_json={
                "component_forecasts": {
                    "crypto_v2": "0.68",
                    "market_implied_v1": "0.52",
                }
            },
        ),
    )
    session.flush()
    insert_market_ranking(
        session,
        {
            "ticker": "SIGNAL-GOOD",
            "ranked_at": now,
            "title": "Will Bitcoin be above 100k by July 31?",
            "status": "open",
            "series_ticker": "KXCRYPTO",
            "forecast_model": "ensemble_v2",
            "forecast_probability": "0.66",
            "best_side": "BUY_YES",
            "best_price": "0.48",
            "estimated_edge": "0.18",
            "liquidity_score": "85",
            "spread_score": "90",
            "time_score": "80",
            "model_confidence_score": "82",
            "opportunity_score": "88",
            "spread": "0.04",
            "liquidity": "12000",
            "time_to_close_minutes": "300",
            "reason": "Seeded Phase 3G signal opportunity.",
        },
    )
    session.flush()
    return forecast


def _paper_decision(forecast_id: int | None) -> PaperDecision:
    return PaperDecision(
        ticker="SIGNAL-GOOD",
        forecast_id=forecast_id,
        model_name="ensemble_v2",
        side=BUY_YES,
        probability=Decimal("0.66"),
        market_price=Decimal("0.48"),
        limit_price=Decimal("0.48"),
        edge=Decimal("0.18"),
        quantity=1,
        reason="Signal attribution paper order.",
        raw_decision_json={
            "forecast_id": forecast_id,
            "strategy": "phase_3g_test",
        },
    )
