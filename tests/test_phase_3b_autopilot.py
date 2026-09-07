from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from kalshi_predictor.autopilot.guardrails import (
    evaluate_opportunity_guardrails,
    evaluate_start_guardrails,
)
from kalshi_predictor.autopilot.repository import (
    complete_autopilot_cycle,
    create_autopilot_cycle,
    create_autopilot_run,
)
from kalshi_predictor.autopilot.runner import run_autopilot_once
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import AutopilotCycle, RiskEvent
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.registry import ForecastRunSummary
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_autopilot_blocked_when_disabled(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = run_autopilot_once(session, settings=Settings(autopilot_enabled=False))
        session.commit()

        event = session.scalar(select(RiskEvent))

    assert result.status == "BLOCKED"
    assert event is not None
    assert event.guardrail_name == "autopilot_enabled"


def test_autopilot_blocked_when_env_not_demo(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = run_autopilot_once(
            session,
            settings=_enabled_settings(kalshi_env="prod"),
        )
        session.commit()

        event = session.scalar(select(RiskEvent))

    assert result.status == "BLOCKED"
    assert event is not None
    assert event.guardrail_name == "kalshi_env"


def test_dry_run_does_not_call_execution_client(tmp_path, monkeypatch) -> None:
    session_factory = _session_factory(tmp_path)
    fake_client = _FakeExecutionClient()
    monkeypatch.setattr(
        "kalshi_predictor.autopilot.runner.run_forecast_models",
        _no_op_forecast_run,
    )
    with session_factory() as session:
        _seed_market(session)

        result = run_autopilot_once(
            session,
            settings=_enabled_settings(),
            execution_client=fake_client,
        )
        session.commit()

        cycle = session.scalar(select(AutopilotCycle))

    assert result.status == "DRY_RUN"
    assert result.orders_attempted == 1
    assert result.orders_submitted == 0
    assert fake_client.called is False
    assert cycle is not None
    assert cycle.summary_json is not None
    assert "dry_run_orders" in cycle.summary_json


def test_stale_data_guardrail_blocks_cycle(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_market(session, captured_at=utc_now() - timedelta(hours=2))

        result = run_autopilot_once(session, settings=_enabled_settings())
        session.commit()

        event = session.scalar(select(RiskEvent))

    assert result.status == "BLOCKED"
    assert event is not None
    assert event.guardrail_name == "fresh_data"


def test_low_edge_guardrail_creates_risk_event(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = evaluate_opportunity_guardrails(
            session,
            opportunity=_candidate(estimated_edge="0.01"),
            settings=_enabled_settings(),
            cycle_orders_attempted=0,
        )
        session.commit()

        event = session.scalar(select(RiskEvent))

    assert result.allowed is False
    assert event is not None
    assert event.guardrail_name == "min_edge"


def test_daily_order_guardrail_blocks(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = _enabled_settings(autopilot_max_daily_orders=1)
    with session_factory() as session:
        _seed_market(session)
        run = create_autopilot_run(session, settings)
        cycle = create_autopilot_cycle(
            session,
            run_id=run.id,
            cycle_number=1,
            settings=settings,
        )
        complete_autopilot_cycle(
            session,
            cycle,
            status="SUBMITTED",
            opportunities_scanned=1,
            orders_attempted=1,
            orders_submitted=1,
            orders_blocked=0,
            stop_reason=None,
            summary={"submitted_orders": [_candidate()]},
        )

        result = evaluate_start_guardrails(session, settings=settings)
        session.commit()

        event = session.scalar(
            select(RiskEvent).where(RiskEvent.guardrail_name == "max_daily_orders")
        )

    assert result.allowed is False
    assert event is not None


def test_cycle_summary_created_when_blocked(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        result = run_autopilot_once(session, settings=Settings(autopilot_enabled=False))
        session.commit()

        cycle = session.scalar(select(AutopilotCycle))

    assert result.status == "BLOCKED"
    assert cycle is not None
    assert cycle.summary_json is not None
    assert "start_guardrails" in cycle.summary_json


def test_autopilot_ui_page_smoke(tmp_path) -> None:
    client = TestClient(
        create_app(
            session_factory=_session_factory(tmp_path),
            settings=Settings(),
        )
    )

    response = client.get("/autopilot")

    assert response.status_code == 200
    assert "Autopilot" in response.text
    assert "Run one dry-run cycle" in response.text


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'autopilot.db'}")
    return get_session_factory(engine)


def _enabled_settings(**overrides) -> Settings:
    values = {
        "kalshi_env": "demo",
        "autopilot_enabled": True,
        "autopilot_dry_run": True,
        "autopilot_model": "market_implied_v1",
        "autopilot_min_edge": Decimal("0.05"),
        "autopilot_min_opportunity_score": Decimal("10"),
        "autopilot_require_fresh_data_minutes": 15,
        "opportunity_min_edge": Decimal("0.03"),
        "opportunity_min_score": Decimal("10"),
        "opportunity_max_spread": Decimal("0.20"),
        "opportunity_min_time_to_close_minutes": Decimal("30"),
        "opportunity_max_results": 20,
    }
    values.update(overrides)
    return Settings(**values)


def _seed_market(session, *, captured_at=None) -> None:
    now = captured_at or utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": "AUTO-TEST",
            "status": "open",
            "title": "Will the autopilot test market resolve yes?",
            "series_ticker": "AUTO",
            "event_ticker": "AUTO-EVENT",
            "close_time": (now + timedelta(hours=4)).isoformat(),
            "volume_fp": "1000",
            "open_interest_fp": "500",
            "liquidity_dollars": "10000",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.50", "10"]],
            }
        },
        now,
    )
    insert_forecast(
        session,
        ForecastOutput(
            ticker="AUTO-TEST",
            forecasted_at=now,
            model_name="market_implied_v1",
            yes_probability=Decimal("0.70"),
            market_mid_probability=None,
            best_yes_bid=Decimal("0.40"),
            best_yes_ask=Decimal(snapshot.best_yes_ask),
            feature_json={"source": "test"},
        ),
    )
    session.flush()


def _candidate(**overrides) -> dict:
    data = {
        "ticker": "AUTO-TEST",
        "model_name": "market_implied_v1",
        "side": "BUY_YES",
        "price": "0.50",
        "forecast_probability": "0.70",
        "estimated_edge": "0.10",
        "opportunity_score": "80",
        "reason": "Seeded test candidate.",
    }
    data.update(overrides)
    return data


def _no_op_forecast_run(*args, **kwargs) -> ForecastRunSummary:
    del args, kwargs
    return ForecastRunSummary(snapshots_scanned=1, forecasts_inserted=0, skipped=0)


class _FakeExecutionClient:
    def __init__(self) -> None:
        self.called = False

    def execute(self, *args, **kwargs) -> dict:
        del args, kwargs
        self.called = True
        return {"status": "CALLED"}
