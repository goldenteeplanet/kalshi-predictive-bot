from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import LearningRejectionLog, PaperOrder
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.learning import diagnostics as learning_diagnostics
from kalshi_predictor.learning.diagnostics import (
    build_learning_diagnostics,
    generate_learning_diagnostics_report,
    threshold_advisor,
)
from kalshi_predictor.learning.repository import insert_learning_rejection
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.paper import strategy as paper_strategy
from kalshi_predictor.paper.simulator import run_paper_trading
from kalshi_predictor.paper.strategy import generate_paper_decisions
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_learning_rejection_log_insert_works(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        inserted = insert_learning_rejection(
            session,
            {
                "ticker": "DIAG-INSERT",
                "model_name": "ensemble_v2",
                "reason": "low_score",
                "edge": Decimal("0.02"),
                "opportunity_score": Decimal("31.88"),
                "spread": Decimal("0.04"),
                "liquidity": Decimal("100"),
                "settlement_eta_hours": Decimal("2"),
                "raw_json": {"source": "test"},
            },
        )
        session.commit()
        stored = session.scalar(
            select(LearningRejectionLog).where(LearningRejectionLog.id == inserted.id)
        )

    assert stored is not None
    assert stored.ticker == "DIAG-INSERT"
    assert stored.reason == "low_score"
    assert stored.opportunity_score == "31.88"


def test_low_score_rejection_is_counted(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        insert_learning_rejection(
            session,
            {
                "ticker": "DIAG-LOW-SCORE",
                "model_name": "ensemble_v2",
                "reason": "low_score",
                "edge": Decimal("0.02"),
                "opportunity_score": Decimal("31.88"),
                "raw_json": {"source": "test"},
            },
        )
        diagnostics = build_learning_diagnostics(session, settings=Settings())

    breakdown = {row["reason"]: row["count"] for row in diagnostics["rejection_breakdown"]}
    assert breakdown["low_score"] == 1


def test_duplicate_rejection_is_counted(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(learning_mode=True, paper_max_open_orders=100)
    with session_factory() as session:
        _, forecast = _seed_snapshot_and_forecast(
            session,
            ticker="DIAG-DUPLICATE",
            model_name="ensemble_v2",
            probability="0.60",
            yes_ask="0.50",
        )
        _seed_ranking(
            session,
            ticker="DIAG-DUPLICATE",
            opportunity_score="50",
            estimated_edge="0.10",
        )
        session.add(
            PaperOrder(
                ticker="DIAG-DUPLICATE",
                forecast_id=forecast.id,
                created_at=utc_now(),
                model_name="ensemble_v2",
                side="BUY_YES",
                probability="0.60",
                market_price="0.50",
                limit_price="0.50",
                edge="0.10",
                quantity=1,
                status="OPEN",
                reason="existing paper trade",
                raw_decision_json="{}",
            )
        )
        session.flush()

        result = generate_paper_decisions(
            session,
            settings=settings,
            model_name="ensemble_v2",
        )
        diagnostics = build_learning_diagnostics(session, settings=settings)

    breakdown = {row["reason"]: row["count"] for row in diagnostics["rejection_breakdown"]}
    assert result.duplicates_skipped == 1
    assert breakdown["duplicate_trade"] == 1


def test_duplicate_cooldown_allows_older_duplicate_if_position_allows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        learning_min_edge=Decimal("0.01"),
        learning_duplicate_cooldown_hours=24,
        learning_max_paper_positions_per_market=3,
        paper_max_position_per_market=3,
        paper_max_open_orders=100,
    )
    with session_factory() as session:
        _, forecast = _seed_snapshot_and_forecast(
            session,
            ticker="DIAG-OLD-DUP",
            model_name="ensemble_v2",
            probability="0.60",
            yes_ask="0.50",
        )
        _seed_ranking(
            session,
            ticker="DIAG-OLD-DUP",
            opportunity_score="50",
            estimated_edge="0.10",
        )
        session.add(
            PaperOrder(
                ticker="DIAG-OLD-DUP",
                forecast_id=forecast.id,
                created_at=utc_now() - timedelta(hours=25),
                model_name="ensemble_v2",
                side="BUY_YES",
                probability="0.60",
                market_price="0.50",
                limit_price="0.50",
                edge="0.10",
                quantity=1,
                status="FILLED",
                reason="older learning paper trade",
                raw_decision_json="{}",
            )
        )
        session.flush()

        summary = run_paper_trading(session, settings=settings, model_name="ensemble_v2")
        orders = list(session.scalars(select(PaperOrder).order_by(PaperOrder.id)))

    assert summary.orders_created == 1
    assert summary.duplicates_skipped == 0
    assert len(orders) == 2
    assert orders[-1].ticker == "DIAG-OLD-DUP"


def test_learning_mode_rotates_past_duplicates_to_create_trades(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        learning_min_edge=Decimal("0.01"),
        learning_min_opportunity_score=Decimal("35"),
        learning_duplicate_cooldown_hours=24,
        learning_candidate_scan_limit=6,
        learning_target_trades_per_cycle=3,
        learning_min_trades_per_cycle=3,
        learning_max_daily_paper_trades=20,
        learning_max_paper_positions_per_market=10,
        paper_max_position_per_market=10,
        paper_max_open_orders=100,
    )
    with session_factory() as session:
        for index, score in enumerate(["90", "80", "70", "60", "50", "40"], start=1):
            ticker = f"DIAG-ROTATE-{index}"
            _, forecast = _seed_snapshot_and_forecast(
                session,
                ticker=ticker,
                model_name="ensemble_v2",
                probability="0.60",
                yes_ask="0.50",
            )
            _seed_ranking(
                session,
                ticker=ticker,
                opportunity_score=score,
                estimated_edge="0.10",
            )
            if index <= 3:
                session.add(
                    PaperOrder(
                        ticker=ticker,
                        forecast_id=forecast.id,
                        created_at=utc_now(),
                        model_name="ensemble_v2",
                        side="BUY_YES",
                        probability="0.60",
                        market_price="0.50",
                        limit_price="0.50",
                        edge="0.10",
                        quantity=1,
                        status="FILLED",
                        reason="recent duplicate",
                        raw_decision_json="{}",
                    )
                )
        session.flush()

        result = generate_paper_decisions(session, settings=settings, model_name="ensemble_v2")

    assert result.duplicates_skipped == 3
    assert result.decisions_generated == 3
    assert {decision.ticker for decision in result.decisions} == {
        "DIAG-ROTATE-4",
        "DIAG-ROTATE-5",
        "DIAG-ROTATE-6",
    }


def test_learning_mode_bulk_loads_phase3ak_gates(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        learning_min_edge=Decimal("0.01"),
        learning_min_opportunity_score=Decimal("35"),
        learning_candidate_scan_limit=6,
        learning_target_trades_per_cycle=3,
        learning_min_trades_per_cycle=3,
        learning_max_daily_paper_trades=20,
        learning_max_paper_positions_per_market=10,
        paper_max_position_per_market=10,
        paper_max_open_orders=100,
    )
    calls: list[list[str]] = []

    def fake_component_provenance(session, *, tickers, include_single_leg=False, limit=None):
        del session, limit
        calls.append(list(tickers))
        assert include_single_leg is True
        return {
            "rows": [
                {
                    "ticker": ticker,
                    "is_multi_leg": False,
                    "learning_eligibility": "NOT_MULTILEG",
                    "learning_eligible": True,
                    "blocking_reason": "single_leg_or_non_sports_market",
                    "component_status_counts": {},
                    "snapshot_status": {},
                }
                for ticker in tickers
            ]
        }

    def fail_per_ticker_lookup(session, ticker):
        del session, ticker
        raise AssertionError("Phase 3AK fallback should not run for bulk-loaded tickers")

    monkeypatch.setattr(
        paper_strategy,
        "build_multi_leg_component_provenance",
        fake_component_provenance,
    )
    monkeypatch.setattr(
        paper_strategy,
        "multi_leg_learning_eligibility",
        fail_per_ticker_lookup,
    )

    with session_factory() as session:
        for index, score in enumerate(["90", "80", "70", "60", "50", "40"], start=1):
            ticker = f"DIAG-BULK-{index}"
            _seed_snapshot_and_forecast(
                session,
                ticker=ticker,
                model_name="ensemble_v2",
                probability="0.60",
                yes_ask="0.50",
            )
            _seed_ranking(
                session,
                ticker=ticker,
                opportunity_score=score,
                estimated_edge="0.10",
            )
        session.flush()

        result = generate_paper_decisions(session, settings=settings, model_name="ensemble_v2")

    assert result.decisions_generated == 3
    assert len(calls) == 1
    assert set(calls[0]) == {f"DIAG-BULK-{index}" for index in range(1, 7)}


def test_threshold_advisor_recommends_lower_score_when_top_score_below_threshold(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        learning_min_edge=Decimal("0.01"),
        learning_min_opportunity_score=Decimal("35"),
    )
    with session_factory() as session:
        _seed_ranking(
            session,
            ticker="DIAG-ADVISOR",
            opportunity_score="31.88",
            estimated_edge="0.02",
        )
        insert_learning_rejection(
            session,
            {
                "ticker": "DIAG-ADVISOR",
                "model_name": "ensemble_v2",
                "reason": "low_score",
                "edge": Decimal("0.02"),
                "opportunity_score": Decimal("31.88"),
                "raw_json": {"source": "test"},
            },
        )

        advisor = threshold_advisor(session, settings=settings)

    assert advisor.observed_top_score == Decimal("31.88")
    assert advisor.recommended_min_score == Decimal("25")
    assert advisor.expected_additional_paper_trades == 1
    assert "Lower min score from 35 to 25" in advisor.message


def test_threshold_advisor_identifies_duplicate_bottleneck(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        learning_min_edge=Decimal("0.01"),
        learning_min_opportunity_score=Decimal("35"),
        learning_duplicate_cooldown_hours=24,
    )
    with session_factory() as session:
        _, forecast = _seed_snapshot_and_forecast(
            session,
            ticker="DIAG-DUP-BOTTLENECK",
            model_name="ensemble_v2",
            probability="0.60",
            yes_ask="0.50",
        )
        _seed_ranking(
            session,
            ticker="DIAG-DUP-BOTTLENECK",
            opportunity_score="31.88",
            estimated_edge="0.02",
        )
        session.add(
            PaperOrder(
                ticker="DIAG-DUP-BOTTLENECK",
                forecast_id=forecast.id,
                created_at=utc_now(),
                model_name="ensemble_v2",
                side="BUY_YES",
                probability="0.60",
                market_price="0.50",
                limit_price="0.50",
                edge="0.10",
                quantity=1,
                status="FILLED",
                reason="recent duplicate",
                raw_decision_json="{}",
            )
        )
        session.flush()

        advisor = threshold_advisor(session, settings=settings)

    assert advisor.recommended_min_score == Decimal("25")
    assert advisor.additional_candidates_available == 1
    assert advisor.duplicate_blocked_additional_candidates == 1
    assert advisor.expected_additional_paper_trades == 0
    assert "duplicate protection is the current bottleneck" in advisor.message


def test_threshold_advisor_accounts_for_phase3ak_safety_blocks(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(
        learning_mode=True,
        learning_min_edge=Decimal("0.01"),
        learning_min_opportunity_score=Decimal("35"),
    )

    def fake_component_provenance(session, *, tickers, include_single_leg=False, limit=None):
        del session, limit
        assert include_single_leg is True
        return {
            "rows": [
                {
                    "ticker": tickers[0],
                    "is_multi_leg": True,
                    "learning_eligibility": "INELIGIBLE",
                    "learning_eligible": False,
                    "blocking_reason": "unsupported_component",
                    "component_status_counts": {"unsupported": 1},
                    "snapshot_status": {"status": "usable_snapshot", "usable": True},
                }
            ]
        }

    monkeypatch.setattr(
        learning_diagnostics,
        "build_multi_leg_component_provenance",
        fake_component_provenance,
    )
    with session_factory() as session:
        _seed_ranking(
            session,
            ticker="DIAG-SAFETY-BLOCK",
            opportunity_score="31.88",
            estimated_edge="0.02",
        )

        advisor = threshold_advisor(session, settings=settings)

    assert advisor.recommended_min_score == Decimal("25")
    assert advisor.safety_blocked_additional_candidates == 1
    assert advisor.expected_additional_paper_trades == 0
    assert "safety gates block" in advisor.message


def test_learning_diagnostics_report_renders(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ranking(
            session,
            ticker="DIAG-REPORT",
            opportunity_score="31.88",
            estimated_edge="0.02",
        )
        insert_learning_rejection(
            session,
            {
                "ticker": "DIAG-REPORT",
                "model_name": "ensemble_v2",
                "reason": "low_score",
                "edge": Decimal("0.02"),
                "opportunity_score": Decimal("31.88"),
                "raw_json": {"category": "crypto"},
            },
        )
        output = generate_learning_diagnostics_report(
            session,
            output_path=tmp_path / "learning_diagnostics.md",
            settings=Settings(),
        )

    text = output.read_text(encoding="utf-8")
    assert "# Learning Funnel Diagnostics" in text
    assert "## Funnel Summary" in text
    assert "## Rejection Breakdown" in text
    assert "low_score" in text
    assert "## Threshold Advisor" in text
    assert "## Top Bottleneck" in text
    assert "## Recommended Next Action" in text


def test_ui_learning_diagnostics_renders(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = Settings(learning_mode=True)
    with session_factory() as session:
        _seed_ranking(
            session,
            ticker="DIAG-UI",
            opportunity_score="31.88",
            estimated_edge="0.02",
        )
        insert_learning_rejection(
            session,
            {
                "ticker": "DIAG-UI",
                "model_name": "ensemble_v2",
                "reason": "low_score",
                "edge": Decimal("0.02"),
                "opportunity_score": Decimal("31.88"),
                "raw_json": {"source": "test"},
            },
        )
        session.commit()

    client = TestClient(create_app(session_factory=session_factory, settings=settings))
    response = client.get("/learning")

    assert response.status_code == 200
    assert "Learning Diagnostics" in response.text
    assert "Markets &rarr; Forecasts" in response.text
    assert "Threshold Advisor" in response.text
    assert "Main bottleneck" in response.text
    assert "low_score" in response.text
    assert "Learning mode is too strict" in response.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'learning_diagnostics.db'}")
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
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=captured_at,
            model_name=model_name,
            yes_probability=Decimal(probability),
            market_mid_probability=Decimal("0.45"),
            best_yes_bid=Decimal("0.40"),
            best_yes_ask=Decimal(yes_ask),
            feature_json={"test": True},
            notes="diagnostics test forecast",
        ),
    )
    session.flush()
    return snapshot, forecast


def _seed_ranking(
    session,
    *,
    ticker: str,
    opportunity_score: str,
    estimated_edge: str,
    model_name: str = "ensemble_v2",
) -> None:
    insert_market_ranking(
        session,
        {
            "ticker": ticker,
            "ranked_at": utc_now(),
            "title": f"Will BTC diagnostics market {ticker} resolve yes?",
            "status": "open",
            "forecast_model": model_name,
            "forecast_probability": "0.60",
            "best_side": "BUY_YES",
            "best_price": "0.50",
            "estimated_edge": estimated_edge,
            "liquidity": "100",
            "liquidity_score": "80",
            "spread": "0.05",
            "spread_score": "90",
            "time_to_close_minutes": "120",
            "time_score": "70",
            "model_confidence_score": "65",
            "opportunity_score": opportunity_score,
            "reason": "Seeded learning diagnostics test.",
        },
    )
    session.flush()
