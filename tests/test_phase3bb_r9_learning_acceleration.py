from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import (
    BacktestRun,
    BacktestTrade,
    Forecast,
    MarketRanking,
    PaperOrder,
    Settlement,
)
from kalshi_predictor.paper.models import ORDER_FILLED
from kalshi_predictor.phase3bb_r9_learning_acceleration import (
    build_phase3bb_r9_learning_acceleration,
    write_phase3bb_r9_learning_acceleration_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3bb_r9_keeps_replay_separate_from_real_paper_learning(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()

    with session_factory() as session:
        forecast = _seed_settled_forecast(session, now=now)
        session.add(
            PaperOrder(
                ticker="KXR9-YES",
                forecast_id=forecast.id,
                created_at=now,
                model_name="crypto_v2",
                side="yes",
                probability="0.80",
                market_price="0.60",
                limit_price="0.61",
                edge="0.20",
                quantity=1,
                status=ORDER_FILLED,
                reason="test filled paper order",
                raw_decision_json=json.dumps({"test": "r9"}),
            )
        )
        _seed_backtest_trade(session, forecast=forecast, now=now)
        session.flush()

        payload = build_phase3bb_r9_learning_acceleration(
            session,
            reports_dir=Path(tmp_path) / "reports",
            limit=100,
        )

    assert payload["paper_learning_counts"]["settled_paper_trades"] == 1
    assert payload["replay_counts"]["backtest_only_rows"] == 1
    assert payload["replay_counts"]["rows_counted_as_real_paper_learning"] == 0
    assert payload["summary"]["replay_rows_counted_as_real_paper_learning"] == 0
    assert payload["safety_flags"]["counts_replay_as_real_paper_learning"] is False
    assert payload["paper_trade_creation"] is False


def test_phase3bb_r9_calibration_uses_settled_forecasts_only(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()

    with session_factory() as session:
        _seed_settled_forecast(session, now=now)
        session.add(
            MarketRanking(
                ticker="KXR9-YES",
                ranked_at=now,
                title="R9 yes test",
                status="closed",
                series_ticker="KXR9",
                event_ticker="KXR9",
                volume="10",
                open_interest="10",
                liquidity="10",
                spread="0.01",
                midpoint="0.60",
                time_to_close_minutes="60",
                forecast_model="crypto_v2",
                forecast_probability="0.80",
                best_side="yes",
                best_price="0.60",
                estimated_edge="0.20",
                liquidity_score="1",
                spread_score="1",
                time_score="1",
                model_confidence_score="1",
                opportunity_score="10",
                reason="test ranking",
                raw_json=json.dumps({"test": "r9"}),
            )
        )
        session.flush()

        payload = build_phase3bb_r9_learning_acceleration(
            session,
            reports_dir=Path(tmp_path) / "reports",
            limit=100,
        )

    rows = {
        row["model_name"]: row for row in payload["model_calibration"]["model_rows"]
    }
    assert rows["crypto_v2"]["evaluable_forecast_rows"] == 1
    assert rows["crypto_v2"]["brier_score"] == "0.0400"
    assert rows["crypto_v2"]["accuracy"] == "1.0000"
    assert rows["crypto_v2"]["positive_edge_win_rate"] == "1.0000"


def test_phase3bb_r9_writes_requested_artifacts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = Path(tmp_path) / "reports"

    with session_factory() as session:
        _seed_settled_forecast(session, now=utc_now())
        session.flush()
        artifacts = write_phase3bb_r9_learning_acceleration_report(
            session,
            output_dir=reports_dir / "phase3bb_r9",
            reports_dir=reports_dir,
            limit=100,
        )

    assert artifacts.executive_summary_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.replay_candidates_csv_path.exists()
    assert artifacts.model_calibration_path.exists()
    assert artifacts.manifest_path.exists()
    assert "crypto_v2" in artifacts.replay_candidates_csv_path.read_text(encoding="utf-8")
    assert "Historical replay remains backtest-only" in artifacts.executive_summary_path.read_text(
        encoding="utf-8"
    )


def test_phase3bb_r9_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r9-learning-acceleration", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r9-learning-acceleration" in result.output
    assert "--limit" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3bb_r9.db'}")
    return get_session_factory(engine)


def _seed_market(session, *, ticker: str, title: str) -> None:
    upsert_market(
        session,
        {
            "ticker": ticker,
            "title": title,
            "event_ticker": ticker,
            "series_ticker": ticker.split("-", 1)[0],
            "status": "closed",
            "close_time": "2026-01-01T19:00:00Z",
            "market_type": "binary",
            "rules_primary": "Test settlement terms.",
        },
    )
    session.flush()


def _seed_settled_forecast(session, *, now):
    _seed_market(session, ticker="KXR9-YES", title="R9 yes test")
    forecast = Forecast(
        ticker="KXR9-YES",
        forecasted_at=now,
        model_name="crypto_v2",
        yes_probability="0.80",
        market_mid_probability="0.60",
        best_yes_bid="0.59",
        best_yes_ask="0.61",
        feature_json=json.dumps({"test": "r9"}),
        notes="test forecast",
    )
    session.add(forecast)
    session.add(
        Settlement(
            ticker="KXR9-YES",
            settled_at=now,
            result="yes",
            yes_settlement_value="1",
            raw_json=json.dumps({"test": "r9"}),
            updated_at=now,
        )
    )
    session.flush()
    return forecast


def _seed_backtest_trade(session, *, forecast: Forecast, now) -> None:
    run = BacktestRun(
        name="r9 test run",
        strategy_name="test_strategy",
        model_name="crypto_v2",
        started_at=now,
        completed_at=now,
        start_time=now,
        end_time=now,
        config_json=json.dumps({"test": "r9"}),
        summary_json=json.dumps({"test": "r9"}),
        notes="test",
    )
    session.add(run)
    session.flush()
    session.add(
        BacktestTrade(
            backtest_run_id=run.id,
            ticker=forecast.ticker,
            forecast_id=forecast.id,
            simulated_at=now,
            side="yes",
            price="0.60",
            quantity=1,
            edge="0.20",
            settlement_result="yes",
            pnl="0.40",
            raw_decision_json=json.dumps({"test": "r9"}),
        )
    )
