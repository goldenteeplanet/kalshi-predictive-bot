from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.confidence.engine import generate_confidence_weights
from kalshi_predictor.confidence.repository import insert_model_confidence_score
from kalshi_predictor.confidence.scoring import score_model_confidence_metrics
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_market,
    upsert_settlement,
)
from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.ensemble_v2 import EnsembleV2Forecaster
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.learning.safety import learning_status, settled_paper_trade_count
from kalshi_predictor.learning.targets import (
    category_priority_score,
    generate_learning_targets,
    learning_priority_score,
    settlement_speed_score,
)
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.paper.ledger import get_paper_summary
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.paper.simulator import run_paper_trading
from kalshi_predictor.tournament.repository import insert_model_weight
from kalshi_predictor.ui import routes as ui_routes
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_learning_settings_lower_paper_and_opportunity_thresholds() -> None:
    settings = Settings(
        learning_mode=True,
        paper_min_edge=Decimal("0.05"),
        opportunity_min_score=Decimal("60"),
        learning_min_edge=Decimal("0.01"),
        learning_min_opportunity_score=Decimal("35"),
    )

    learned = learning_paper_settings(settings)

    assert learned.paper_min_edge == Decimal("0.01")
    assert learned.opportunity_min_edge == Decimal("0.01")
    assert learned.opportunity_min_score == Decimal("35")
    assert learned.execution_enabled is False
    assert learned.execution_dry_run is True


def test_learning_mode_defaults_enabled_for_primary_paper_generation() -> None:
    settings = Settings()

    assert settings.learning_mode is True
    assert settings.learning_target_settled_trades == 500
    assert settings.learning_min_edge == Decimal("0.01")
    assert settings.learning_min_opportunity_score == Decimal("35")
    assert settings.learning_max_daily_paper_trades == 100
    assert settings.learning_max_paper_positions_per_market == 3
    assert settings.learning_min_trades_per_cycle == 5
    assert settings.learning_target_trades_per_cycle == 10
    assert settings.learning_prioritize_fast_settlement is True
    assert settings.learning_max_days_to_settlement == 3
    assert settings.learning_block_demo_execution is True
    assert settings.learning_block_live_execution is True


def test_learning_mode_increases_paper_trades_with_default_thresholds(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    strict_settings = Settings(learning_mode=False, paper_min_edge=Decimal("0.05"))
    learning_settings = Settings()
    with session_factory() as session:
        for index in range(12):
            _seed_snapshot_and_forecast(
                session,
                ticker=f"LEARN-INCREASE-{index}",
                model_name="ensemble_v2",
                probability="0.515",
                yes_ask="0.50",
            )

        strict = run_paper_trading(
            session,
            settings=strict_settings,
            model_name="ensemble_v2",
        )
        learning = run_paper_trading(
            session,
            settings=learning_settings,
            model_name="ensemble_v2",
        )

    assert strict.orders_created == 0
    assert learning.orders_created == 10


def test_learning_mode_creates_lower_threshold_paper_trade_and_blocks_duplicate(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        paper_min_edge=Decimal("0.05"),
        learning_min_edge=Decimal("0.01"),
        learning_max_daily_paper_trades=10,
    )
    with session_factory() as session:
        _seed_snapshot_and_forecast(
            session,
            ticker="LEARN-EDGE",
            model_name="ensemble_v2",
            probability="0.53",
            yes_ask="0.50",
        )

        first = run_paper_trading(session, settings=settings, model_name="ensemble_v2")
        second = run_paper_trading(session, settings=settings, model_name="ensemble_v2")
        summary = get_paper_summary(session)
        order = session.scalar(select(PaperOrder).where(PaperOrder.ticker == "LEARN-EDGE"))

    assert first.orders_created == 1
    assert second.orders_created == 0
    assert second.duplicates_skipped == 1
    assert summary.total_orders == 1
    assert order is not None
    assert "Learning Mode" in order.reason


def test_learning_mode_daily_paper_trade_cap(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        learning_min_edge=Decimal("0.01"),
        learning_max_daily_paper_trades=0,
    )
    with session_factory() as session:
        _seed_snapshot_and_forecast(
            session,
            ticker="LEARN-CAP",
            model_name="ensemble_v2",
            probability="0.55",
            yes_ask="0.50",
        )

        result = run_paper_trading(session, settings=settings, model_name="ensemble_v2")

    assert result.orders_created == 0
    assert result.skipped_due_to_risk_limits == 1


def test_learning_status_counts_exact_value_settlements(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_settled_paper_order(session, "LEARN-BINARY", result="yes")
        _seed_settled_paper_order(
            session,
            "LEARN-SCALAR",
            result="scalar",
            yes_settlement_value="0.76",
        )
        _seed_settled_paper_order(session, "LEARN-VALUE", yes_settlement_value="0")
        _seed_settled_paper_order(
            session,
            "LEARN-BLANK-SCALAR",
            result="scalar",
            yes_settlement_value="",
        )

        count = settled_paper_trade_count(session)
        status = learning_status(session, settings=Settings())

    assert count == 3
    assert status["settled_paper_trades"] == 3


def test_learning_targets_prioritize_fast_settlement_and_filter_long_dated(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        learning_max_days_to_settlement=3,
        learning_allowed_categories="crypto,general",
    )
    with session_factory() as session:
        _seed_ranking(session, ticker="BTC-FAST", title="Will BTC close above 100000?", minutes=120)
        _seed_ranking(
            session,
            ticker="PRESIDENT-FAST",
            title="Will this presidential election market resolve yes?",
            minutes=120,
        )
        _seed_ranking(
            session,
            ticker="BTC-SLOW",
            title="Will BTC close above 200000?",
            minutes=10 * 1440,
        )

        result = generate_learning_targets(
            session,
            settings=settings,
            model_name="ensemble_v2",
            limit=10,
        )

    assert result.inserted == 2
    assert result.targets[0]["ticker"] == "BTC-FAST"
    assert result.targets[-1]["ticker"] == "PRESIDENT-FAST"
    assert settlement_speed_score(Decimal("60")) > settlement_speed_score(Decimal("10080"))
    assert category_priority_score(category="weather", market_text="temperature") > (
        category_priority_score(category="general", market_text="presidential election")
    )
    assert learning_priority_score(
        edge=Decimal("0.05"),
        opportunity_score=Decimal("70"),
        confidence_score=Decimal("65"),
        speed_score=Decimal("95"),
    ) > learning_priority_score(
        edge=Decimal("0.05"),
        opportunity_score=Decimal("70"),
        confidence_score=Decimal("65"),
        speed_score=Decimal("10"),
    )


def test_confidence_labels_and_dynamic_weights_normalize() -> None:
    settings = Settings(model_confidence_min_settled_trades=25)
    needs_data = score_model_confidence_metrics(
        _confidence_row("new_model", settled=5),
        settings=settings,
    )
    underperforming = score_model_confidence_metrics(
        _confidence_row("bad_model", settled=30, brier="0.42", roi="-0.10"),
        settings=settings,
    )
    leader = score_model_confidence_metrics(
        _confidence_row("leader_model", settled=30, brier="0.10", roi="0.25"),
        settings=settings,
    )
    leader["confidence_label"] = "Leader"

    weights = generate_confidence_weights(
        [needs_data, underperforming, leader],
        settings=settings,
        lookback_days=30,
    )
    crypto_weights = [row for row in weights if row["category"] == "crypto"]
    total = sum((Decimal(row["weight"]) for row in crypto_weights), Decimal("0"))

    assert needs_data["confidence_label"] == "Needs More Data"
    assert underperforming["confidence_label"] == "Underperforming"
    assert leader["confidence_score"] > Decimal("70")
    assert total == Decimal("1.0000")
    assert any(row["model_name"] == "leader_model" for row in crypto_weights)
    assert not any(row["model_name"] == "bad_model" for row in crypto_weights)


def test_ensemble_v2_uses_model_confidence_weights(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        snapshot = _seed_snapshot_and_forecast(
            session,
            ticker="BTC-CONFIDENCE",
            model_name="market_implied_v1",
            probability="0.40",
            yes_ask="0.50",
        )
        _seed_forecast(session, snapshot, "crypto_v2", "0.80")
        insert_model_weight(
            session,
            {
                "model_name": "market_implied_v1",
                "category": "crypto",
                "weight": "0.25",
                "method": "model_confidence_v1",
                "lookback_days": 30,
            },
        )
        insert_model_weight(
            session,
            {
                "model_name": "crypto_v2",
                "category": "crypto",
                "weight": "0.75",
                "method": "model_confidence_v1",
                "lookback_days": 30,
            },
        )

        forecast = EnsembleV2Forecaster().forecast(session, snapshot)

    assert forecast is not None
    assert forecast.yes_probability == Decimal("0.7000")
    assert Decimal(forecast.feature_json["weights_used"]["crypto_v2"]) == Decimal("0.75")


def test_ui_learning_and_model_confidence_pages_render(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(learning_mode=True, execution_enabled=True, execution_dry_run=False)
    with session_factory() as session:
        insert_model_confidence_score(
            session,
            {
                **_confidence_row("ensemble_v2", settled=30, brier="0.10", roi="0.20"),
                "sample_size_score": "100",
                "calibration_score": "75",
                "profitability_score": "70",
                "drawdown_score": "90",
                "confidence_score": "78",
                "confidence_label": "Leader",
                "status": "OK",
                "notes": "test leader",
            },
        )
        _seed_snapshot_and_forecast(
            session,
            ticker="UI-LEARN",
            model_name="ensemble_v2",
            probability="0.60",
            yes_ask="0.50",
        )
        _seed_ranking(session, ticker="UI-LEARN", title="Will BTC UI resolve yes?", minutes=120)
        session.commit()

    client = TestClient(create_app(session_factory=session_factory, settings=settings))

    learning = client.get("/learning")
    dashboard = client.get("/")
    settings_page = client.get("/settings")
    confidence = client.get("/models/confidence")
    blocked = client.post("/demo-execute/UI-LEARN?confirmation=DEMO%20ONLY")

    assert learning.status_code == 200
    assert "Learning Mode" in learning.text
    assert "Blocked Opportunities" in learning.text
    assert dashboard.status_code == 200
    assert "Expected completion" in dashboard.text
    assert settings_page.status_code == 200
    assert "Target Settled Trades" in settings_page.text
    assert confidence.status_code == 200
    assert "Model Confidence Engine" in confidence.text
    assert blocked.json()["status"] == "LEARNING_BLOCKED"


def test_learning_run_once_action_success_returns_json(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)

    def fake_run_learning_once(session, *, settings):
        return SimpleNamespace(
            run_id=1,
            cycle_id=2,
            status="COMPLETED",
            markets_scanned=100,
            forecasts_generated=1230,
            opportunities_found=40,
            paper_trades_created=5,
            settlements_synced=3,
            settled_paper_trades_total=25,
            errors=[],
        )

    monkeypatch.setattr(ui_routes, "run_learning_once", fake_run_learning_once)
    monkeypatch.setattr(ui_routes, "generate_learning_report", lambda session, *, settings: None)

    client = TestClient(
        create_app(
            session_factory=session_factory,
            settings=Settings(learning_mode=True, overnight_require_market_data=False),
        )
    )
    response = client.post("/learning/run-once")
    payload = response.json()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert payload["ok"] is True
    assert payload["message"] == "Learning cycle completed."
    assert payload["summary"]["paper_trades_created"] == 5
    assert payload["summary"]["forecasts_evaluated"] == 1230
    assert payload["summary"]["opportunities_found"] == 40


def test_learning_run_once_action_failure_returns_json(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)

    def fake_run_learning_once(session, *, settings):
        raise RuntimeError("database is busy")

    monkeypatch.setattr(ui_routes, "run_learning_once", fake_run_learning_once)

    client = TestClient(
        create_app(
            session_factory=session_factory,
            settings=Settings(learning_mode=True, overnight_require_market_data=False),
        )
    )
    response = client.post("/learning/run-once")
    payload = response.json()

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert payload["ok"] is False
    assert payload["message"] == "Learning cycle failed."
    assert payload["error"] == "database is busy"
    assert "learning-once" in payload["next_action"]


def test_learning_run_once_daily_cap_reached_returns_json(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        learning_max_daily_paper_trades=100,
        overnight_require_market_data=False,
    )
    with session_factory() as session:
        _seed_daily_paper_orders(session, count=100)
        session.commit()

    def fake_run_learning_once(session, *, settings):
        raise AssertionError("learning cycle should not run when daily cap is reached")

    monkeypatch.setattr(ui_routes, "run_learning_once", fake_run_learning_once)

    client = TestClient(create_app(session_factory=session_factory, settings=settings))
    response = client.post("/learning/run-once")
    payload = response.json()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert payload == {
        "ok": False,
        "message": "Daily learning paper trade cap reached: 100 / 100.",
        "next_action": "Wait until tomorrow or increase LEARNING_MAX_DAILY_PAPER_TRADES.",
    }


def test_learning_button_disabled_when_daily_cap_reached(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        learning_max_daily_paper_trades=100,
        overnight_require_market_data=False,
    )
    with session_factory() as session:
        _seed_daily_paper_orders(session, count=100)
        session.commit()

    client = TestClient(create_app(session_factory=session_factory, settings=settings))
    response = client.get("/learning")

    assert response.status_code == 200
    assert "Daily learning paper trade cap reached: 100 / 100." in response.text
    assert "Learning cap reached. The bot will continue syncing settlements" in response.text
    assert "Run one learning cycle</button>" in response.text
    assert "disabled" in response.text


def test_action_js_handles_non_json_responses_gracefully() -> None:
    script = Path("src/kalshi_predictor/ui/static/app.js").read_text(encoding="utf-8")

    assert "response.ok" in script
    assert "content-type" in script
    assert "response.text()" in script
    assert "Action returned non-JSON response" in script
    assert "[data-action-message]" in script
    assert "Running..." in script
    assert "userFacingActionError" in script
    assert "Could not reach the learning action endpoint" in script
    assert "Action failed:" not in script


def test_phase_3f_learning_cli_help() -> None:
    runner = CliRunner()
    for command in (
        "learning-status",
        "learning-once",
        "learning-run",
        "learning-report",
        "model-confidence",
        "learning-targets",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'learning.db'}")
    return get_session_factory(engine)


def _seed_snapshot_and_forecast(
    session,
    *,
    ticker: str,
    model_name: str,
    probability: str,
    yes_ask: str,
):
    captured_at = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": f"Will {ticker} resolve yes?",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": yes_ask,
            "liquidity_dollars": "100",
            "close_time": (captured_at + timedelta(hours=2)).isoformat(),
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.50", "10"]],
            }
        },
        captured_at,
    )
    _seed_forecast(session, snapshot, model_name, probability)
    session.flush()
    return snapshot


def _seed_forecast(session, snapshot, model_name: str, probability: str) -> None:
    insert_forecast(
        session,
        ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=model_name,
            yes_probability=Decimal(probability),
            market_mid_probability=Decimal("0.45"),
            best_yes_bid=Decimal("0.40"),
            best_yes_ask=Decimal(snapshot.best_yes_ask or "0.50"),
            feature_json={"test": True},
            notes="BTC crypto test forecast",
        ),
    )
    session.flush()


def _seed_ranking(session, *, ticker: str, title: str, minutes: int) -> None:
    insert_market_ranking(
        session,
        {
            "ticker": ticker,
            "ranked_at": utc_now(),
            "title": title,
            "status": "open",
            "forecast_model": "ensemble_v2",
            "forecast_probability": "0.60",
            "best_side": "BUY_YES",
            "best_price": "0.50",
            "estimated_edge": "0.10",
            "liquidity": "100",
            "liquidity_score": "80",
            "spread": "0.05",
            "spread_score": "90",
            "time_to_close_minutes": str(minutes),
            "time_score": "70",
            "model_confidence_score": "65",
            "opportunity_score": "75",
            "reason": "Seeded learning target test.",
        },
    )
    session.flush()


def _seed_settled_paper_order(
    session,
    ticker: str,
    *,
    result: str | None = None,
    yes_settlement_value: str | None = None,
) -> None:
    upsert_market(session, {"ticker": ticker, "status": "finalized"})
    session.add(
        PaperOrder(
            ticker=ticker,
            forecast_id=None,
            created_at=utc_now(),
            model_name="ensemble_v2",
            side="BUY_YES",
            probability="0.60",
            market_price="0.50",
            limit_price="0.50",
            edge="0.10",
            quantity=1,
            status=ORDER_FILLED,
            reason="settled count fixture",
            raw_decision_json="{}",
        )
    )
    settlement = {
        "ticker": ticker,
        "status": "finalized",
        "settlement_ts": "2026-01-02T00:00:00Z",
    }
    if result is not None:
        settlement["result"] = result
    if yes_settlement_value is not None:
        settlement["yes_settlement_value"] = yes_settlement_value
    upsert_settlement(session, settlement)
    session.flush()


def _seed_daily_paper_orders(session, *, count: int) -> None:
    now = utc_now()
    for index in range(count):
        session.add(
            PaperOrder(
                ticker=f"LEARN-DAILY-CAP-{index}",
                forecast_id=None,
                created_at=now,
                model_name="ensemble_v2",
                side="BUY_YES",
                probability="0.60",
                market_price="0.50",
                limit_price="0.50",
                edge="0.10",
                quantity=1,
                status="OPEN",
                reason="daily cap fixture",
                raw_decision_json="{}",
            )
        )
    session.flush()


def _confidence_row(
    model_name: str,
    *,
    settled: int,
    brier: str | None = "0.20",
    roi: str | None = "0.10",
) -> dict:
    return {
        "model_name": model_name,
        "category": "crypto",
        "lookback_days": 30,
        "forecast_count": settled,
        "evaluated_forecast_count": settled,
        "paper_trade_count": settled,
        "settled_trade_count": settled,
        "brier_score": brier,
        "log_loss": "0.50",
        "win_rate": "0.60",
        "roi_on_exposure": roi,
        "total_pnl": "5",
        "max_drawdown": "0.10",
        "raw_json": {},
    }
