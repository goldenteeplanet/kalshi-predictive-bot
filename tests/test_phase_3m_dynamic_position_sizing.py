from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_settlement,
)
from kalshi_predictor.data.schema import PaperOrder, PaperPosition, PositionSizingDecisionLog
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.paper.ledger import create_paper_order
from kalshi_predictor.paper.models import BUY_YES, ORDER_FILLED, PaperDecision
from kalshi_predictor.paper.simulator import run_paper_trading
from kalshi_predictor.position_sizing.sizer import (
    ConfidenceTier,
    DynamicPositionSizer,
    FactorWeights,
    PositionSizingConfig,
    PositionSizingInput,
    SizingMode,
)


def test_live_low_medium_high_tiers_execute_expected_sizes() -> None:
    sizer = DynamicPositionSizer(_sizer_config())

    low = sizer.decide(_sizing_input(confidence_score=0.55, opportunity_score=0.55))
    medium = sizer.decide(
        _sizing_input(confidence_score=0.75, opportunity_score=0.70, liquidity_score=0.65)
    )
    high = sizer.decide(_sizing_input())

    assert low.tier == ConfidenceTier.LOW
    assert low.executed_contracts == 1
    assert medium.tier == ConfidenceTier.MEDIUM
    assert medium.executed_contracts == 3
    assert high.tier == ConfidenceTier.HIGH
    assert high.executed_contracts == 5


@pytest.mark.parametrize(
    ("liquidity_score", "expected"),
    [(0.69, 3), (0.44, 1)],
)
def test_liquidity_caps_high_tier(liquidity_score: float, expected: int) -> None:
    decision = DynamicPositionSizer(_sizer_config()).decide(
        _sizing_input(
            confidence_score=0.99,
            opportunity_score=0.99,
            liquidity_score=liquidity_score,
            current_drawdown_fraction=0.0,
            historical_accuracy=1.0,
            historical_sample_size=100,
        )
    )

    assert decision.proposed_contracts == 5
    assert decision.executed_contracts == expected
    assert "LIQUIDITY_CAP_APPLIED" in decision.reason_codes


@pytest.mark.parametrize(
    ("current_drawdown_fraction", "expected"),
    [(0.55, 3), (0.80, 1), (1.0, 0)],
)
def test_drawdown_caps_and_kill_switch(
    current_drawdown_fraction: float,
    expected: int,
) -> None:
    decision = DynamicPositionSizer(_sizer_config()).decide(
        _sizing_input(current_drawdown_fraction=current_drawdown_fraction)
    )

    assert decision.executed_contracts == expected
    if expected == 0:
        assert decision.tier == ConfidenceTier.BLOCKED
        assert "DRAWDOWN_KILL_SWITCH" in decision.reason_codes


@pytest.mark.parametrize(
    ("external_risk_cap", "expected"),
    [(4, 3), (2, 1), (0, 0)],
)
def test_external_caps_bucket_down_without_rounding_up(
    external_risk_cap: int,
    expected: int,
) -> None:
    decision = DynamicPositionSizer(_sizer_config()).decide(
        _sizing_input(external_risk_cap=external_risk_cap)
    )

    assert decision.live_candidate_contracts == expected
    assert decision.executed_contracts == expected


def test_missing_external_risk_cap_defaults_live_to_one() -> None:
    decision = DynamicPositionSizer(_sizer_config()).decide(
        _sizing_input(external_risk_cap=None)
    )

    assert decision.proposed_contracts == 5
    assert decision.live_candidate_contracts == 1
    assert decision.executed_contracts == 1
    assert "MISSING_EXTERNAL_RISK_CAP" in decision.reason_codes


def test_history_shrinkage_and_minimum_sample_cap() -> None:
    decision = DynamicPositionSizer(_sizer_config()).decide(
        _sizing_input(historical_accuracy=1.0, historical_sample_size=20)
    )

    assert decision.adjusted_historical_accuracy == pytest.approx(0.75)
    assert decision.live_candidate_contracts == 3
    assert "INSUFFICIENT_HISTORY_FOR_HIGH" in decision.reason_codes


def test_invalid_input_falls_back_to_one_and_hard_block_returns_zero() -> None:
    sizer = DynamicPositionSizer(_sizer_config())

    invalid = sizer.decide(_sizing_input(confidence_score=float("nan")))
    blocked = sizer.decide(_sizing_input(hard_risk_block=True))

    assert invalid.fallback_used is True
    assert invalid.executed_contracts == 1
    assert "INVALID_INPUT" in invalid.reason_codes
    assert blocked.tier == ConfidenceTier.BLOCKED
    assert blocked.executed_contracts == 0


def test_shadow_and_disabled_modes_preserve_one_contract_execution() -> None:
    shadow = DynamicPositionSizer(_sizer_config(mode=SizingMode.SHADOW)).decide(_sizing_input())
    disabled = DynamicPositionSizer(_sizer_config(mode=SizingMode.DISABLED)).decide(
        _sizing_input()
    )

    assert shadow.proposed_contracts == 5
    assert shadow.live_candidate_contracts == 5
    assert shadow.executed_contracts == 1
    assert disabled.proposed_contracts == 5
    assert disabled.executed_contracts == 1


def test_same_input_is_deterministic_and_sizes_are_discrete() -> None:
    sizer = DynamicPositionSizer(_sizer_config())
    item = _sizing_input()

    first = sizer.decide(item)
    second = sizer.decide(item)

    assert first == second
    assert first.executed_contracts in {0, 1, 3, 5}
    assert first.live_candidate_contracts <= first.proposed_contracts
    assert first.live_candidate_contracts <= min(first.caps.values())


def test_invalid_configuration_fails() -> None:
    with pytest.raises(ValueError):
        DynamicPositionSizer(
            _sizer_config(weights=FactorWeights(confidence=1.0, opportunity=1.0))
        )


def test_disabled_paper_trading_preserves_one_contract_and_logs_decision(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast(session, ticker="PHASE3M-DISABLED", probability=Decimal("0.95"))

        summary = run_paper_trading(session, settings=_settings("disabled"))
        session.commit()

        order = session.scalar(select(PaperOrder))
        sizing_log = session.scalar(select(PositionSizingDecisionLog))

    assert summary.orders_created == 1
    assert order is not None
    assert order.quantity == 1
    assert order.status == ORDER_FILLED
    assert sizing_log is not None
    assert sizing_log.mode == "disabled"
    assert sizing_log.paper_order_id == order.id


def test_live_paper_trading_can_execute_five_with_valid_history_and_risk_cap(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_history(session, wins=30, losses=0)
        _seed_forecast(session, ticker="PHASE3M-HIGH", probability=Decimal("0.95"))

        summary = run_paper_trading(
            session,
            settings=_settings(
                "live",
                dynamic_position_sizing_live_max_contracts=5,
                dynamic_position_sizing_external_risk_cap=5,
            ),
        )
        session.commit()

        order = session.scalar(
            select(PaperOrder).where(PaperOrder.ticker == "PHASE3M-HIGH")
        )
        position = session.get(PaperPosition, "PHASE3M-HIGH")
        sizing_log = session.scalar(
            select(PositionSizingDecisionLog)
            .where(PositionSizingDecisionLog.ticker == "PHASE3M-HIGH")
            .order_by(PositionSizingDecisionLog.id.desc())
        )

    assert summary.orders_created == 1
    assert order is not None
    assert order.quantity == 5
    assert position is not None
    assert position.yes_contracts == 5
    assert sizing_log is not None
    assert sizing_log.tier == "high"
    assert sizing_log.executed_contracts == 5


def test_live_paper_trading_missing_external_risk_cap_stays_at_one(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_history(session, wins=30, losses=0)
        _seed_forecast(session, ticker="PHASE3M-RISK-MISSING", probability=Decimal("0.95"))

        run_paper_trading(
            session,
            settings=_settings(
                "live",
                dynamic_position_sizing_live_max_contracts=5,
                dynamic_position_sizing_external_risk_cap=None,
            ),
        )
        session.commit()

        order = session.scalar(
            select(PaperOrder).where(PaperOrder.ticker == "PHASE3M-RISK-MISSING")
        )
        sizing_log = session.scalar(
            select(PositionSizingDecisionLog)
            .where(PositionSizingDecisionLog.ticker == "PHASE3M-RISK-MISSING")
            .order_by(PositionSizingDecisionLog.id.desc())
        )

    assert order is not None
    assert order.quantity == 1
    assert sizing_log is not None
    assert sizing_log.proposed_contracts == 5
    assert sizing_log.live_candidate_contracts == 1
    assert "MISSING_EXTERNAL_RISK_CAP" in sizing_log.reason_codes_json


def test_direct_create_paper_order_uses_sizer_and_keeps_quantity_consistent(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        forecast = _seed_forecast(
            session,
            ticker="PHASE3M-DIRECT",
            probability=Decimal("0.95"),
        )
        decision = _paper_decision("PHASE3M-DIRECT", forecast.id)

        order = create_paper_order(
            session,
            decision,
            settings=_settings(
                "live",
                dynamic_position_sizing_live_max_contracts=5,
                dynamic_position_sizing_external_risk_cap=5,
            ),
        )
        session.commit()

        sizing_log = session.scalar(select(PositionSizingDecisionLog))

    assert order is not None
    assert order.quantity == 3
    assert sizing_log is not None
    assert sizing_log.paper_order_id == order.id
    assert sizing_log.reason_codes_json


def test_historical_accuracy_excludes_future_settlements(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_history(session, wins=0, losses=5)
        _seed_history(
            session,
            wins=30,
            losses=0,
            prefix="FUTURE",
            settled_at=datetime(2027, 1, 3, tzinfo=UTC),
        )
        forecast = _seed_forecast(
            session,
            ticker="PHASE3M-NO-LOOKAHEAD",
            probability=Decimal("0.95"),
        )
        decision = _paper_decision("PHASE3M-NO-LOOKAHEAD", forecast.id)

        order = create_paper_order(
            session,
            decision,
            settings=_settings(
                "live",
                dynamic_position_sizing_live_max_contracts=5,
                dynamic_position_sizing_external_risk_cap=5,
            ),
        )
        session.commit()

        sizing_log = session.scalar(
            select(PositionSizingDecisionLog)
            .where(PositionSizingDecisionLog.ticker == "PHASE3M-NO-LOOKAHEAD")
            .order_by(PositionSizingDecisionLog.id.desc())
        )

    assert order is not None
    assert order.quantity < 5
    assert sizing_log is not None
    assert sizing_log.historical_sample_size == 5


def _sizer_config(
    mode: SizingMode = SizingMode.LIVE,
    **overrides,
) -> PositionSizingConfig:
    values = {
        "mode": mode,
        "live_max_contracts": 5,
        "global_max_contracts": 5,
    }
    values.update(overrides)
    return PositionSizingConfig(**values)


def _sizing_input(**overrides) -> PositionSizingInput:
    values = {
        "confidence_score": 0.92,
        "opportunity_score": 0.88,
        "liquidity_score": 0.85,
        "current_drawdown_fraction": 0.10,
        "max_drawdown_fraction": 1.0,
        "historical_accuracy": 0.70,
        "historical_sample_size": 80,
        "external_risk_cap": 5,
        "margin_cap": 5,
        "portfolio_cap": 5,
        "decision_timestamp": datetime(2026, 1, 2, tzinfo=UTC),
    }
    values.update(overrides)
    return PositionSizingInput(**values)


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'phase3m.db'}")
    return get_session_factory(engine)


def _settings(mode: str, **overrides) -> Settings:
    values = {
        "learning_mode": False,
        "paper_min_edge": Decimal("0.05"),
        "paper_max_order_quantity": 1,
        "paper_max_position_per_market": 5,
        "paper_max_open_orders": 100,
        "paper_allow_buy_no": True,
        "autopilot_max_daily_drawdown": Decimal("5.00"),
        "dynamic_position_sizing_mode": mode,
    }
    values.update(overrides)
    return Settings(**values)


def _seed_forecast(
    session,
    *,
    ticker: str,
    probability: Decimal,
    captured_at: datetime | None = None,
):
    now = captured_at or datetime(2026, 1, 2, tzinfo=UTC)
    insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": f"Will {ticker} resolve yes?",
            "close_time": (now + timedelta(hours=4)).isoformat(),
            "volume_fp": "3000",
            "open_interest_fp": "2000",
            "liquidity_dollars": "50000",
            "yes_bid_dollars": "0.09",
            "yes_ask_dollars": "0.10",
            "no_bid_dollars": "0.89",
            "no_ask_dollars": "0.90",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.09", "100"], ["0.08", "80"]],
                "no_dollars": [["0.89", "100"], ["0.88", "80"]],
            }
        },
        now,
    )
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=now + timedelta(minutes=1),
            model_name="market_implied_v1",
            yes_probability=probability,
            market_mid_probability=None,
            best_yes_bid=Decimal("0.09"),
            best_yes_ask=Decimal("0.10"),
            feature_json={"source": "phase3m-test"},
        ),
    )
    session.flush()
    return forecast


def _seed_history(
    session,
    *,
    wins: int,
    losses: int,
    prefix: str = "HIST",
    settled_at: datetime | None = None,
) -> None:
    settled = settled_at or datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(wins + losses):
        ticker = f"{prefix}-{index}"
        forecast = _seed_forecast(
            session,
            ticker=ticker,
            probability=Decimal("0.95"),
            captured_at=settled - timedelta(hours=2, minutes=index),
        )
        result = "yes" if index < wins else "no"
        session.add(
            PaperOrder(
                ticker=ticker,
                forecast_id=forecast.id,
                created_at=settled - timedelta(hours=1, minutes=index),
                model_name="market_implied_v1",
                side=BUY_YES,
                probability="0.95",
                market_price="0.10",
                limit_price="0.10",
                edge="0.85",
                quantity=1,
                status=ORDER_FILLED,
                reason="historical test order",
                raw_decision_json="{}",
            )
        )
        upsert_settlement(
            session,
            {
                "ticker": ticker,
                "result": result,
                "settlement_ts": settled.isoformat(),
            },
        )
    session.flush()


def _paper_decision(ticker: str, forecast_id: int | None) -> PaperDecision:
    return PaperDecision(
        ticker=ticker,
        forecast_id=forecast_id,
        model_name="market_implied_v1",
        side=BUY_YES,
        probability=Decimal("0.95"),
        market_price=Decimal("0.10"),
        limit_price=Decimal("0.10"),
        edge=Decimal("0.85"),
        quantity=1,
        reason="direct paper decision",
        raw_decision_json={"strategy": "paper_edge_v1"},
    )
