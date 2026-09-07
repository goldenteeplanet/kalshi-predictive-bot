from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.autopilot.runner import run_autopilot_once
from kalshi_predictor.cli import app
from kalshi_predictor.confidence.engine import run_model_confidence_engine
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_settlement,
)
from kalshi_predictor.data.schema import (
    AutopilotPaperTrade,
    LearningPaperTrade,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.lanes.metrics import refresh_autopilot_metrics, refresh_learning_metrics
from kalshi_predictor.lanes.repository import (
    insert_autopilot_paper_trade,
    insert_learning_paper_trade,
)
from kalshi_predictor.learning.accelerator import accelerate_learning
from kalshi_predictor.paper.ledger import create_paper_order
from kalshi_predictor.paper.models import BUY_YES, PaperDecision
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_learning_trades_are_separated_from_autopilot_trades(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_min_edge=Decimal("0.02"),
        learning_min_opportunity_score=Decimal("1"),
        autopilot_enabled=True,
        autopilot_dry_run=True,
        autopilot_model="ensemble_v2",
        autopilot_min_edge=Decimal("0.03"),
        autopilot_min_opportunity_score=Decimal("1"),
        autopilot_require_fresh_data_minutes=999,
        autopilot_max_orders_per_cycle=1,
        learning_mode=True,
    )
    with session_factory() as session:
        learn_snapshot = _seed_snapshot(session, ticker="LANE-LEARN")
        _seed_forecast(
            session,
            ticker=learn_snapshot.ticker,
            model_name="ensemble_v2",
            probability="0.60",
            forecasted_at=learn_snapshot.captured_at,
        )
        auto_snapshot = _seed_snapshot(session, ticker="LANE-AUTO")
        _seed_forecast(
            session,
            ticker=auto_snapshot.ticker,
            model_name="market_implied_v1",
            probability="0.66",
            forecasted_at=auto_snapshot.captured_at,
        )

        learning_result = accelerate_learning(session, settings=settings, limit=10)
        autopilot_result = run_autopilot_once(session, settings=settings)
        session.commit()

        learning_trades = session.scalar(select(func.count(LearningPaperTrade.id)))
        autopilot_trades = session.scalar(select(func.count(AutopilotPaperTrade.id)))

    assert learning_result.paper_trades_created >= 1
    assert learning_result.learning_paper_trades_inserted >= 1
    assert autopilot_result.status == "DRY_RUN"
    assert learning_trades and learning_trades >= 1
    assert autopilot_trades and autopilot_trades >= 1


def test_learning_metrics_are_not_included_in_autopilot_roi(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_settlement(session, {"ticker": "LANE-LEARN", "result": "yes"})
        insert_learning_paper_trade(
            session,
            {
                "ticker": "LANE-LEARN",
                "model_name": "ensemble_v2",
                "side": BUY_YES,
                "price": Decimal("0.40"),
                "quantity": 1,
                "edge": Decimal("0.20"),
                "status": "FILLED",
            },
        )
        insert_autopilot_paper_trade(
            session,
            {
                "ticker": "LANE-AUTO",
                "model_name": "ensemble_v2",
                "side": BUY_YES,
                "price": Decimal("0.40"),
                "quantity": 1,
                "edge": Decimal("0.20"),
                "status": "DRY_RUN",
            },
        )
        learning_metric = refresh_learning_metrics(session, settings=Settings())
        autopilot_metric = refresh_autopilot_metrics(session, settings=Settings())

    assert learning_metric.settled_trade_count == 1
    assert Decimal(learning_metric.roi_on_exposure or "0") > 0
    assert autopilot_metric.settled_trade_count == 0
    assert autopilot_metric.roi_on_exposure is None


def test_confidence_engine_uses_only_settled_results_for_roi(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        settled_snapshot = _seed_snapshot(session, ticker="CONF-SETTLED")
        settled_forecast = _seed_forecast(
            session,
            ticker=settled_snapshot.ticker,
            model_name="ensemble_v2",
            probability="0.70",
            forecasted_at=settled_snapshot.captured_at,
        )
        unsettled_snapshot = _seed_snapshot(session, ticker="CONF-OPEN")
        unsettled_forecast = _seed_forecast(
            session,
            ticker=unsettled_snapshot.ticker,
            model_name="ensemble_v2",
            probability="0.70",
            forecasted_at=unsettled_snapshot.captured_at,
        )
        create_paper_order(session, _paper_decision("CONF-SETTLED", settled_forecast.id))
        create_paper_order(session, _paper_decision("CONF-OPEN", unsettled_forecast.id))
        upsert_settlement(session, {"ticker": "CONF-SETTLED", "result": "yes"})

        result = run_model_confidence_engine(session, settings=Settings(), persist=False)
        row = next(
            item
            for item in result.rows
            if item["model_name"] == "ensemble_v2" and item["category"] == "general"
        )

    assert row["settled_trade_count"] == 1
    assert row["total_pnl"] == Decimal("0.6")
    assert row["roi_on_exposure"] == Decimal("1.5")


def test_control_center_renders_correctly(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        refresh_learning_metrics(session, settings=Settings())
        refresh_autopilot_metrics(session, settings=Settings())
        session.commit()

    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))
    response = client.get("/control-center")

    assert response.status_code == 200
    assert "Control Center" in response.text
    assert "Learning Mode Status" in response.text
    assert "Autopilot Status" in response.text
    assert "Model Confidence Engine" in response.text


def test_dual_lane_cli_help() -> None:
    runner = CliRunner()
    for command in ("accelerate-learning", "control-center-report"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3f1_lanes.db'}")
    return get_session_factory(engine)


def _seed_snapshot(session, *, ticker: str):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": f"Will {ticker} resolve yes?",
            "series_ticker": "KXGENERAL",
            "event_ticker": "KXGENERAL-EVENT",
            "close_time": (now + timedelta(hours=2)).isoformat(),
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
            "liquidity_dollars": "100",
            "volume_fp": "100",
            "open_interest_fp": "50",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "20"]],
                "no_dollars": [["0.50", "20"]],
            }
        },
        now,
    )


def _seed_forecast(
    session,
    *,
    ticker: str,
    model_name: str,
    probability: str,
    forecasted_at,
):
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=forecasted_at,
            model_name=model_name,
            yes_probability=Decimal(probability),
            market_mid_probability=Decimal("0.45"),
            best_yes_bid=Decimal("0.40"),
            best_yes_ask=Decimal("0.50"),
            feature_json={"test": "dual_lanes"},
            notes="general test forecast",
        ),
    )
    session.flush()
    return forecast


def _paper_decision(ticker: str, forecast_id: int | None) -> PaperDecision:
    return PaperDecision(
        ticker=ticker,
        forecast_id=forecast_id,
        model_name="ensemble_v2",
        side=BUY_YES,
        probability=Decimal("0.70"),
        market_price=Decimal("0.40"),
        limit_price=Decimal("0.40"),
        edge=Decimal("0.30"),
        quantity=1,
        reason="Dual lane confidence test.",
        raw_decision_json={"source": "dual_lane_test"},
    )
