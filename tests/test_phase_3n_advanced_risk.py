from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from kalshi_predictor.advanced_risk.engine import (
    AdvancedRiskConfig,
    AdvancedRiskEngine,
    AdvancedRiskMode,
    MarketRiskSnapshot,
    PortfolioRiskSnapshot,
    TradeEdgeStatistics,
)
from kalshi_predictor.advanced_risk.reports import generate_advanced_risk_report
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_settlement,
)
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    AdvancedRiskReservation,
    PaperOrder,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.paper.ledger import create_paper_order
from kalshi_predictor.paper.models import BUY_YES, ORDER_FILLED, PaperDecision
from kalshi_predictor.paper.simulator import run_paper_trading
from kalshi_predictor.ui.app import create_app


def test_engine_never_increases_phase_3m_and_buckets_caps_down() -> None:
    request = _request(phase_3m_contracts=5)
    decision = AdvancedRiskEngine(_config(live_max_contracts=4)).decide(request)

    assert decision.live_candidate_contracts == 3
    assert decision.executed_contracts == 3
    assert decision.live_candidate_contracts <= request.phase_3m_proposed_contracts
    assert decision.executed_contracts in {0, 1, 3, 5}


def test_category_caps_reduce_and_block() -> None:
    reduce_to_three = AdvancedRiskEngine(_config()).decide(
        _request(category_open_risk={"crypto": Decimal("999.2")})
    )
    reduce_to_one = AdvancedRiskEngine(_config()).decide(
        _request(category_open_risk={"crypto": Decimal("999.6")})
    )
    blocked = AdvancedRiskEngine(_config()).decide(
        _request(category_open_risk={"crypto": Decimal("999.9")})
    )

    assert reduce_to_three.live_candidate_contracts == 3
    assert "CATEGORY_CAP_APPLIED" in reduce_to_three.reason_codes
    assert reduce_to_one.live_candidate_contracts == 1
    assert blocked.live_candidate_contracts == 0
    assert blocked.executed_contracts == 0
    assert "CATEGORY_LIMIT_REACHED" in blocked.hard_blocks


def test_daily_loss_and_drawdown_caps() -> None:
    daily_block = AdvancedRiskEngine(_config()).decide(
        _request(realized_pnl_session=Decimal("-600"))
    )
    drawdown_three = AdvancedRiskEngine(_config()).decide(
        _request(account_equity=Decimal("9000"), high_water_equity=Decimal("10000"))
    )
    drawdown_one = AdvancedRiskEngine(_config()).decide(
        _request(account_equity=Decimal("8500"), high_water_equity=Decimal("10000"))
    )

    assert daily_block.executed_contracts == 0
    assert "DAILY_LOSS_LIMIT_REACHED" in daily_block.hard_blocks
    assert drawdown_three.executed_contracts == 3
    assert drawdown_one.executed_contracts == 1


def test_spread_and_quote_hard_blocks() -> None:
    spread_block = AdvancedRiskEngine(_config()).decide(
        _request(market=_market(bid=Decimal("0.10"), ask=Decimal("0.30")))
    )
    stale_block = AdvancedRiskEngine(_config()).decide(
        _request(market=_market(quote_age_ms=9999999))
    )

    assert spread_block.executed_contracts == 0
    assert "SPREAD_LIMIT_EXCEEDED" in spread_block.hard_blocks
    assert stale_block.executed_contracts == 0
    assert "QUOTE_STALE" in stale_block.hard_blocks


def test_fractional_kelly_and_negative_kelly_caps() -> None:
    kelly_config = _config(
        kelly_enabled=True,
        max_applied_kelly_fraction=Decimal("0.00004"),
        max_trade_risk_fraction=Decimal("0.00004"),
    )
    reduced = AdvancedRiskEngine(kelly_config).decide(_request())
    blocked = AdvancedRiskEngine(_config(kelly_enabled=True)).decide(
        _request(edge_statistics=_edge(raw_win_probability=Decimal("0.10")))
    )

    assert reduced.live_candidate_contracts == 1
    assert "KELLY_CAP_APPLIED" in reduced.reason_codes
    assert blocked.live_candidate_contracts == 0
    assert "KELLY_NONPOSITIVE" in blocked.reason_codes


def test_nonpositive_risk_adjusted_ev_blocks_live() -> None:
    decision = AdvancedRiskEngine(_config(ev_enabled=True)).decide(
        _request(
            edge_statistics=_edge(
                raw_win_probability=Decimal("0.40"),
                average_gross_win_per_contract=Decimal("0.10"),
                average_gross_loss_per_contract=Decimal("0.90"),
            )
        )
    )

    assert decision.executed_contracts == 0
    assert "NONPOSITIVE_RISK_ADJUSTED_EV" in decision.hard_blocks


def test_shadow_mode_computes_block_but_preserves_phase_3m_quantity() -> None:
    decision = AdvancedRiskEngine(_config(mode=AdvancedRiskMode.SHADOW)).decide(
        _request(market=_market(bid=Decimal("0.10"), ask=Decimal("0.30")))
    )

    assert decision.live_candidate_contracts == 0
    assert decision.executed_contracts == 5
    assert decision.action.value == "BLOCK"
    assert "MODE_SHADOW" in decision.reason_codes


def test_live_paper_order_routes_through_advanced_risk_and_reservation(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_history(session, wins=35, losses=0)
        _seed_forecast(session, ticker="PHASE3N-LIVE", probability=Decimal("0.95"))

        summary = run_paper_trading(
            session,
            settings=_settings(
                dynamic_position_sizing_live_max_contracts=5,
                dynamic_position_sizing_external_risk_cap=5,
                advanced_risk_engine_mode="live",
                advanced_risk_live_max_contracts=3,
            ),
        )
        session.commit()

        order = session.scalar(select(PaperOrder).where(PaperOrder.ticker == "PHASE3N-LIVE"))
        risk_log = session.scalar(select(AdvancedRiskDecisionLog))
        reservation = session.scalar(select(AdvancedRiskReservation))

    assert summary.orders_created == 1
    assert order is not None
    assert order.quantity == 3
    assert risk_log is not None
    assert risk_log.action == "REDUCE"
    assert risk_log.paper_order_id == order.id
    assert reservation is not None
    assert reservation.paper_order_id == order.id
    assert reservation.status == "FILLED"


def test_advanced_risk_check_is_idempotent_for_same_decision(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_history(session, wins=35, losses=0)
        forecast = _seed_forecast(
            session,
            ticker="PHASE3N-IDEMPOTENT",
            probability=Decimal("0.95"),
        )
        decision = _paper_decision("PHASE3N-IDEMPOTENT", forecast.id)
        settings = _settings(
            dynamic_position_sizing_live_max_contracts=5,
            dynamic_position_sizing_external_risk_cap=5,
            advanced_risk_engine_mode="live",
            advanced_risk_live_max_contracts=3,
        )

        first_order = create_paper_order(session, decision, settings=settings)
        second_order = create_paper_order(session, decision, settings=settings)
        session.commit()

        decision_count = session.scalar(select(AdvancedRiskDecisionLog))
        reservation_count = len(list(session.scalars(select(AdvancedRiskReservation))))

    assert first_order is not None
    assert second_order is None
    assert decision_count is not None
    assert reservation_count == 1


def test_advanced_risk_report_and_ui_render(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    settings = _settings()
    with session_factory() as session:
        path = generate_advanced_risk_report(
            session,
            output_path=Path(tmp_path) / "advanced_risk_report.md",
            settings=settings,
        )

    client = TestClient(create_app(session_factory=session_factory, settings=settings))
    dashboard = client.get("/")
    settings_page = client.get("/settings")

    assert path.exists()
    assert "Advanced Risk Engine Report" in path.read_text(encoding="utf-8")
    assert dashboard.status_code == 200
    assert "Advanced Risk Engine" in dashboard.text
    assert settings_page.status_code == 200
    assert "Advanced Risk Engine" in settings_page.text


def _config(
    mode: AdvancedRiskMode = AdvancedRiskMode.LIVE,
    **overrides,
) -> AdvancedRiskConfig:
    values = {
        "mode": mode,
        "live_max_contracts": 5,
        "global_max_contracts": 5,
        "kelly_minimum_sample_size": 1,
        "ev_minimum_sample_size": 1,
    }
    values.update(overrides)
    return AdvancedRiskConfig(**values)


def _request(**overrides):
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)
    phase_contracts = overrides.pop("phase_3m_contracts", 5)
    values = {
        "version": "3N",
        "decision_timestamp": now,
        "trade_intent_id": "intent-1",
        "order_correlation_id": "intent-1",
        "strategy_id": "paper_edge_v1",
        "model_id": "ensemble_v2",
        "category_id": "crypto",
        "instrument_id": "BTC-TEST",
        "correlation_group_id": "crypto",
        "direction": "LONG",
        "phase_3m_tier": {1: "LOW", 3: "MEDIUM", 5: "HIGH"}[phase_contracts],
        "phase_3m_proposed_contracts": phase_contracts,
        "confidence_score": Decimal("0.95"),
        "entry_price": Decimal("0.20"),
        "stop_price": Decimal("0"),
        "point_value": Decimal("1"),
        "tick_size": Decimal("0.01"),
        "estimated_round_trip_fees": Decimal("0"),
        "estimated_slippage_per_contract": Decimal("0"),
        "gap_or_tail_buffer_per_contract": Decimal("0"),
        "portfolio_snapshot": _portfolio(now=now, **_portfolio_overrides(overrides)),
        "market_snapshot": overrides.pop("market", _market()),
        "edge_statistics": overrides.pop("edge_statistics", _edge()),
        "external_hard_risk_block": False,
        "external_margin_cap": None,
        "external_buying_power_cap": None,
    }
    values.update(overrides)
    return values["__class__"](**values) if "__class__" in values else _advanced_request(values)


def _advanced_request(values):
    from kalshi_predictor.advanced_risk.engine import AdvancedRiskRequest

    return AdvancedRiskRequest(**values)


def _portfolio_overrides(values: dict) -> dict:
    keys = {
        "account_equity",
        "high_water_equity",
        "realized_pnl_session",
        "unrealized_pnl_session",
        "category_open_risk",
    }
    return {key: values.pop(key) for key in list(values) if key in keys}


def _portfolio(now: datetime, **overrides) -> PortfolioRiskSnapshot:
    values = {
        "snapshot_id": "snapshot-1",
        "snapshot_version": "v1",
        "captured_at": now,
        "account_equity": Decimal("10000"),
        "start_of_session_equity": Decimal("10000"),
        "high_water_equity": Decimal("10000"),
        "realized_pnl_session": Decimal("0"),
        "unrealized_pnl_session": Decimal("0"),
        "current_total_open_risk": Decimal("0"),
        "current_pending_reserved_risk": Decimal("0"),
        "category_open_risk": {},
        "category_pending_reserved_risk": {},
        "model_open_risk": {},
        "model_pending_reserved_risk": {},
        "instrument_open_risk": {},
        "instrument_pending_reserved_risk": {},
    }
    values.update(overrides)
    return PortfolioRiskSnapshot(**values)


def _market(
    *,
    bid: Decimal = Decimal("0.19"),
    ask: Decimal = Decimal("0.20"),
    quote_age_ms: int = 0,
) -> MarketRiskSnapshot:
    now = datetime(2026, 1, 2, 12, tzinfo=UTC)
    return MarketRiskSnapshot(
        captured_at=now,
        bid_price=bid,
        ask_price=ask,
        quote_age_ms=quote_age_ms,
        executable_depth_contracts=Decimal("100"),
        depth_price_band_ticks=Decimal("5"),
        recent_volume_contracts=Decimal("10000"),
        recent_volume_window_seconds=86400,
        average_daily_volume_contracts=Decimal("10000"),
        open_interest_contracts=Decimal("10000"),
        market_status="OPEN",
        data_quality_status="VALID",
    )


def _edge(**overrides) -> TradeEdgeStatistics:
    values = {
        "bucket_key": "global",
        "bucket_level": "global",
        "sample_size": 100,
        "raw_win_probability": Decimal("0.70"),
        "average_gross_win_per_contract": Decimal("0.80"),
        "average_gross_loss_per_contract": Decimal("0.20"),
        "statistics_as_of": datetime(2026, 1, 1, tzinfo=UTC),
        "outcome_basis": "GROSS",
    }
    values.update(overrides)
    return TradeEdgeStatistics(**values)


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3n.db'}")
    return get_session_factory(engine)


def _settings(**overrides) -> Settings:
    values = {
        "learning_mode": False,
        "paper_min_edge": Decimal("0.05"),
        "paper_max_order_quantity": 1,
        "paper_max_position_per_market": 5,
        "paper_max_open_orders": 100,
        "paper_allow_buy_no": True,
        "autopilot_max_daily_drawdown": Decimal("5.00"),
        "dynamic_position_sizing_mode": "live",
        "dynamic_position_sizing_live_max_contracts": 5,
        "dynamic_position_sizing_external_risk_cap": 5,
        "advanced_risk_engine_mode": "disabled",
        "advanced_risk_quote_max_age_ms": 99999999999,
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
    now = captured_at or datetime(2026, 1, 2, 12, tzinfo=UTC)
    insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": f"Will {ticker} resolve yes?",
            "close_time": (now + timedelta(hours=4)).isoformat(),
            "volume_fp": "3000",
            "volume_24h_fp": "3000",
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
            feature_json={"source": "phase3n-test"},
        ),
    )
    session.flush()
    return forecast


def _seed_history(session, *, wins: int, losses: int) -> None:
    settled = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(wins + losses):
        ticker = f"PHASE3N-HIST-{index}"
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
