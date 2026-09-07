from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_market,
    upsert_settlement,
)
from kalshi_predictor.data.schema import Market, PaperFill, PaperOrder, PaperPnl, PaperPosition
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.paper.ledger import (
    reset_paper_data,
    update_position_for_fill,
    upsert_position,
)
from kalshi_predictor.paper.models import BUY_YES, ORDER_FILLED
from kalshi_predictor.paper.pnl import calculate_and_store_pnl, calculate_settled_pnl
from kalshi_predictor.paper.settlement_reconciliation import build_paper_settlement_reconciliation
from kalshi_predictor.paper.simulator import run_paper_trading


def test_position_weighted_average_updates_correctly(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_position(
            session,
            ticker="AVG",
            yes_contracts=2,
            avg_yes_price=Decimal("0.40"),
        )
        fill = PaperFill(
            paper_order_id=1,
            ticker="AVG",
            filled_at=datetime(2026, 1, 1, tzinfo=UTC),
            side=BUY_YES,
            price="0.60",
            quantity=1,
            fee="0",
            raw_fill_json="{}",
        )

        position = update_position_for_fill(session, fill)

        assert position.yes_contracts == 3
        assert Decimal(position.avg_yes_price) == Decimal("0.4666666666666666666666666667")


def test_filled_orders_update_position(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_forecast(session, ticker="FILL_ME", yes_probability=Decimal("0.60"))

        summary = run_paper_trading(session, settings=_settings())
        session.commit()

        assert summary.orders_created == 1
        assert summary.fills_created == 1
        order = session.scalar(select(PaperOrder))
        position = session.get(PaperPosition, "FILL_ME")
        assert order.status == ORDER_FILLED
        assert position.yes_contracts == 1
        assert Decimal(position.avg_yes_price) == Decimal("0.50")


def test_settled_yes_pnl_works(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        position = upsert_position(
            session,
            ticker="YES_SETTLED",
            yes_contracts=2,
            no_contracts=1,
            avg_yes_price=Decimal("0.40"),
            avg_no_price=Decimal("0.30"),
        )

        assert calculate_settled_pnl(position, "yes") == Decimal("0.90")


def test_settled_no_pnl_works(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        position = upsert_position(
            session,
            ticker="NO_SETTLED",
            yes_contracts=1,
            no_contracts=3,
            avg_yes_price=Decimal("0.70"),
            avg_no_price=Decimal("0.20"),
        )

        assert calculate_settled_pnl(position, "no") == Decimal("1.70")


def test_value_only_exact_settlement_realizes_paper_pnl(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "KXVALUE-ONLY"
        upsert_market(session, {"ticker": ticker, "status": "finalized"})
        upsert_position(
            session,
            ticker=ticker,
            yes_contracts=1,
            avg_yes_price=Decimal("0.40"),
        )
        session.add(
            PaperOrder(
                ticker=ticker,
                forecast_id=None,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                model_name="market_implied_v1",
                side=BUY_YES,
                probability="0.60",
                market_price="0.40",
                limit_price="0.40",
                edge="0.20",
                quantity=1,
                status=ORDER_FILLED,
                reason="value-only settlement test",
                raw_decision_json="{}",
            )
        )
        upsert_settlement(
            session,
            {
                "ticker": ticker,
                "status": "finalized",
                "yes_settlement_value": "1",
                "settlement_ts": "2026-01-02T00:00:00Z",
            },
        )

        summary = calculate_and_store_pnl(session)
        reconciliation = build_paper_settlement_reconciliation(session)

        latest = session.scalar(select(PaperPnl).where(PaperPnl.ticker == ticker))
        assert summary.pnl_rows_inserted == 1
        assert latest.settlement_result == "yes"
        assert Decimal(latest.realized_pnl) == Decimal("0.60")
        assert reconciliation["rows"][0]["reason"] == "ALREADY_REALIZED"


def test_scalar_exact_settlement_value_realizes_paper_pnl(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "KXSCALAR-VALUE"
        upsert_market(session, {"ticker": ticker, "status": "finalized"})
        upsert_position(
            session,
            ticker=ticker,
            yes_contracts=1,
            avg_yes_price=Decimal("0.40"),
        )
        session.add(
            PaperOrder(
                ticker=ticker,
                forecast_id=None,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                model_name="market_implied_v1",
                side=BUY_YES,
                probability="0.60",
                market_price="0.40",
                limit_price="0.40",
                edge="0.20",
                quantity=1,
                status=ORDER_FILLED,
                reason="scalar settlement test",
                raw_decision_json="{}",
            )
        )
        upsert_settlement(
            session,
            {
                "ticker": ticker,
                "status": "finalized",
                "result": "scalar",
                "yes_settlement_value": "0.76",
                "settlement_ts": "2026-01-02T00:00:00Z",
            },
        )

        summary = calculate_and_store_pnl(session)
        reconciliation = build_paper_settlement_reconciliation(session)

        latest = session.scalar(select(PaperPnl).where(PaperPnl.ticker == ticker))
        assert summary.pnl_rows_inserted == 1
        assert latest.settlement_result == "scalar"
        assert Decimal(latest.realized_pnl) == Decimal("0.36")
        assert latest.notes == "settled market realized paper P&L"
        assert reconciliation["summary"]["eligible_to_settle_now"] == 0
        assert reconciliation["rows"][0]["reason"] == "ALREADY_REALIZED"


def test_paper_reset_does_not_delete_market_data(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(session, {"ticker": "KEEP", "status": "open"})
        _seed_forecast(session, ticker="RESET", yes_probability=Decimal("0.60"))
        run_paper_trading(session, settings=_settings())
        upsert_settlement(session, {"ticker": "RESET", "result": "yes"})
        session.commit()

        reset_paper_data(session)
        session.commit()

        assert len(session.scalars(select(Market)).all()) >= 1
        assert len(session.scalars(select(PaperOrder)).all()) == 0
        assert len(session.scalars(select(PaperFill)).all()) == 0
        assert len(session.scalars(select(PaperPosition)).all()) == 0


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'paper_ledger.db'}")
    return get_session_factory(engine)


def _settings() -> Settings:
    return Settings(
        paper_min_edge=Decimal("0.05"),
        paper_max_order_quantity=1,
        paper_max_position_per_market=5,
        paper_max_open_orders=100,
        paper_allow_buy_no=True,
    )


def _seed_forecast(session, *, ticker: str, yes_probability: Decimal):
    market_json = {
        "ticker": ticker,
        "status": "open",
        "yes_bid_dollars": "0.48",
        "yes_ask_dollars": "0.50",
        "last_price_dollars": "0.49",
    }
    insert_market_snapshot(
        session,
        market_json,
        {
            "orderbook_fp": {
                "yes_dollars": [["0.48", "1"]],
                "no_dollars": [["0.50", "1"]],
            }
        },
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    forecast = insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            model_name="market_implied_v1",
            yes_probability=yes_probability,
            market_mid_probability=None,
            best_yes_bid=Decimal("0.48"),
            best_yes_ask=Decimal("0.50"),
            feature_json={"source": "test"},
        ),
    )
    session.flush()
    return forecast
