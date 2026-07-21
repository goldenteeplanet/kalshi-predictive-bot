from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import event, select

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    insert_forecast,
    insert_market_snapshot,
    upsert_settlement,
)
from kalshi_predictor.data.schema import (
    AlertEvent,
    MarketLeg,
    PaperPnl,
    PortfolioSnapshot,
    PositionHistory,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.paper.ledger import upsert_position
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.workstation.reports import (
    generate_analytics_report,
    generate_daily_briefing,
)
from kalshi_predictor.workstation.repository import (
    add_market_to_watchlist,
    alerts_summary,
    create_portfolio_snapshot,
    ensure_default_watchlists,
    evaluate_alerts,
    market_monitor_rows,
    paper_liquidity_plan,
    portfolio_summary,
    portfolio_summary_fast,
    record_position_history,
    remove_market_from_watchlist,
)


def test_portfolio_calculations_and_snapshots(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_position_market(session)
        history = record_position_history(session)
        snapshot = create_portfolio_snapshot(session)
        summary = portfolio_summary(session)
        session.commit()

        stored_history = session.scalar(select(PositionHistory))
        stored_snapshot = session.scalar(select(PortfolioSnapshot))

    assert len(history) == 1
    assert snapshot.total_positions == 1
    assert stored_history is not None
    assert stored_snapshot is not None
    assert summary["open_positions"] == 1
    assert Decimal(summary["total_exposure"]) > 0


def test_fast_portfolio_separates_local_composite_backlog(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_position(
            session,
            ticker="PHASE3D-DIRECT",
            yes_contracts=2,
            no_contracts=0,
            avg_yes_price=Decimal("0.25"),
            avg_no_price=None,
            realized_pnl=Decimal("1"),
        )
        upsert_position(
            session,
            ticker="KXMVECROSSCATEGORY-S2026061EFE9C280-188CAFAB7E0",
            yes_contracts=1,
            no_contracts=0,
            avg_yes_price=Decimal("0"),
            avg_no_price=None,
            realized_pnl=Decimal("2"),
        )
        resolved_ticker = "KXMVESPORTSMULTIGAMEEXTENDED-S2026LOCAL-REALIZED"
        upsert_position(
            session,
            ticker=resolved_ticker,
            yes_contracts=1,
            no_contracts=0,
            avg_yes_price=Decimal("0"),
            avg_no_price=None,
            realized_pnl=Decimal("3"),
        )
        upsert_settlement(
            session,
            {
                "ticker": resolved_ticker,
                "status": "settled",
                "result": "yes",
                "settlement_ts": utc_now().isoformat(),
            },
        )
        session.add(
            PaperPnl(
                ticker=resolved_ticker,
                calculated_at=utc_now(),
                yes_contracts=1,
                no_contracts=0,
                avg_yes_price="0",
                avg_no_price=None,
                settlement_result="yes",
                realized_pnl="3",
                unrealized_pnl="0",
                total_pnl="3",
                notes="settled market realized paper P&L",
            )
        )
        session.flush()
        summary = portfolio_summary_fast(session, positions_limit=10, series_limit=2)

    assert summary["open_positions"] == 1
    assert summary["raw_active_positions"] == 3
    assert summary["local_composite_total_count"] == 2
    assert summary["local_composite_resolved_count"] == 1
    assert summary["local_composite_backlog_count"] == 1
    assert summary["realized_pnl"] == "6"
    assert [row["ticker"] for row in summary["positions"]] == ["PHASE3D-DIRECT"]
    assert summary["positions"][0]["is_local_derived_composite"] is False
    assert summary["local_composite_backlog"][0]["is_local_derived_composite"] is True
    assert "not direct Kalshi exchange markets" in summary["local_composite_notice"]


def test_portfolio_route_labels_local_composite_backlog(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_position(
            session,
            ticker="KXMVESPORTSMULTIGAMEEXTENDED-S2026LOCAL-ABC123",
            yes_contracts=1,
            no_contracts=0,
            avg_yes_price=Decimal("0"),
            avg_no_price=None,
            realized_pnl=Decimal("0"),
        )
        session.commit()
    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))

    response = client.get("/portfolio")

    assert response.status_code == 200
    assert "Direct Paper Positions" in response.text
    assert "Composite Backlog" in response.text
    assert "No exchange-backed paper positions are open" in response.text
    assert "Local Composite Settlement Backlog" in response.text
    assert "Waiting for guarded exact local composite settlement" in response.text


def test_watchlists_add_and_remove(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        watchlist = ensure_default_watchlists(session)[0]
        item = add_market_to_watchlist(
            session,
            watchlist_id=watchlist.id,
            ticker="PHASE3D-TEST",
        )
        removed = remove_market_from_watchlist(session, item_id=item.id)
        session.commit()

    assert item.id is not None
    assert removed == 1


def test_alerts_create_events_from_rankings(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_position_market(session)
        events = evaluate_alerts(session)
        session.commit()

        stored_event = session.scalar(select(AlertEvent))
        summary = alerts_summary(session)

    assert events
    assert stored_event is not None
    assert summary["open_count"] >= 1


def test_daily_briefing_and_analytics_reports(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_position_market(session)
        daily = generate_daily_briefing(
            session,
            output_path=Path(tmp_path) / "daily.md",
            settings=Settings(overnight_require_market_data=False),
        )
        analytics = generate_analytics_report(session, output_path=Path(tmp_path) / "analytics.md")
        session.commit()

    assert daily.exists()
    assert "Daily Briefing" in daily.read_text(encoding="utf-8")
    assert analytics.exists()
    assert "Analytics Report" in analytics.read_text(encoding="utf-8")


def test_workstation_pages_render(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_position_market(session)
        session.commit()
    client = TestClient(create_app(session_factory=session_factory, settings=Settings()))

    for path, marker in (
        ("/portfolio", "Portfolio"),
        ("/positions/PHASE3D-TEST", "Current Position"),
        ("/models", "Model Performance Center"),
        ("/markets", "Market Monitor"),
        ("/analytics", "Performance Analytics"),
        ("/watchlists", "Watchlists"),
        ("/alerts", "Alerts"),
        ("/settings", "Paper Liquidity Plan"),
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert marker in response.text


def test_paper_liquidity_plan_uses_starting_capital(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_position_market(session)
        plan = paper_liquidity_plan(
            session,
            settings=Settings(
                paper_liquidity_starting_capital=Decimal("100"),
                paper_liquidity_growth_target=Decimal("250"),
                paper_liquidity_max_position_fraction=Decimal("0.10"),
            ),
        )

    assert plan["mode"] == "PAPER ONLY"
    assert plan["starting_capital"] == "100"
    assert Decimal(plan["available_liquidity"]) >= Decimal("0")
    assert Decimal(plan["max_new_position"]) <= Decimal(plan["current_equity"]) * Decimal("0.10")


def test_market_monitor_uses_snapshot_fallback_for_zero_ranking_values(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        insert_market_snapshot(
            session,
            {
                "ticker": "PHASE3D-FALLBACK",
                "status": "open",
                "title": "Will the market monitor fallback test resolve yes?",
                "series_ticker": "KXTEST",
                "event_ticker": "KXTEST-EVENT",
                "close_time": (now + timedelta(hours=2)).isoformat(),
                "liquidity_dollars": "5000",
                "volume_fp": "100",
                "open_interest_fp": "75",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.41", "20"]],
                    "no_dollars": [["0.53", "20"]],
                }
            },
            now,
        )
        insert_market_ranking(
            session,
            {
                "ticker": "PHASE3D-FALLBACK",
                "ranked_at": now,
                "title": "Will the market monitor fallback test resolve yes?",
                "status": "open",
                "forecast_model": "ensemble_v2",
                "forecast_probability": "0.66",
                "best_side": "BUY_YES",
                "best_price": "0",
                "midpoint": "0",
                "estimated_edge": "0.18",
                "liquidity_score": "85",
                "spread_score": "90",
                "time_score": "80",
                "model_confidence_score": "82",
                "opportunity_score": "88",
                "spread": "0",
                "liquidity": "0",
                "time_to_close_minutes": "120",
                "reason": "Seeded workstation fallback opportunity.",
            },
        )
        rows = market_monitor_rows(session)

    row = next(item for item in rows if item["ticker"] == "PHASE3D-FALLBACK")
    assert row["current_price"] == "0.44"
    assert row["spread"] == "0.06"
    assert row["liquidity"] == "5000"
    assert row["data_freshness"] == now.isoformat()


def test_market_monitor_shows_na_when_numeric_data_is_missing(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        insert_market_ranking(
            session,
            {
                "ticker": "PHASE3D-MISSING",
                "ranked_at": now,
                "title": "Will the market monitor missing data test resolve yes?",
                "status": "open",
                "forecast_model": "ensemble_v2",
                "forecast_probability": "0.66",
                "best_side": "BUY_YES",
                "best_price": "0",
                "midpoint": "0",
                "estimated_edge": "0.18",
                "liquidity_score": "0",
                "spread_score": "0",
                "time_score": "80",
                "model_confidence_score": "82",
                "opportunity_score": "88",
                "spread": "0",
                "liquidity": "0",
                "time_to_close_minutes": "120",
                "reason": "Seeded workstation missing data opportunity.",
            },
        )
        rows = market_monitor_rows(session)

    row = next(item for item in rows if item["ticker"] == "PHASE3D-MISSING")
    assert row["current_price"] == "n/a"
    assert row["spread"] == "n/a"
    assert row["liquidity"] == "n/a"
    assert row["data_freshness"] == now.isoformat()


def test_market_monitor_flags_stale_and_expired_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        for ticker, captured_at, close_time, status in (
            (
                "PHASE3D-STALE",
                now - timedelta(minutes=16),
                now + timedelta(hours=2),
                "open",
            ),
            (
                "PHASE3D-EXPIRED",
                now,
                now - timedelta(minutes=1),
                "closed",
            ),
        ):
            insert_market_snapshot(
                session,
                {
                    "ticker": ticker,
                    "status": status,
                    "title": f"Will {ticker} resolve yes?",
                    "series_ticker": "KXTEST",
                    "close_time": close_time.isoformat(),
                    "liquidity_dollars": "1000",
                },
                {
                    "orderbook_fp": {
                        "yes_dollars": [["0.40", "10"]],
                        "no_dollars": [["0.50", "10"]],
                    }
                },
                captured_at,
            )
            insert_market_ranking(
                session,
                {
                    "ticker": ticker,
                    "ranked_at": captured_at,
                    "title": f"Will {ticker} resolve yes?",
                    "status": status,
                    "forecast_model": "ensemble_v2",
                    "forecast_probability": "0.60",
                    "best_side": "BUY_YES",
                    "best_price": "0.40",
                    "estimated_edge": "0.20",
                    "liquidity_score": "80",
                    "spread_score": "80",
                    "time_score": "80",
                    "model_confidence_score": "80",
                    "opportunity_score": "80",
                    "spread": "0.10",
                    "liquidity": "1000",
                    "reason": "Freshness truth regression fixture.",
                },
            )
        rows = market_monitor_rows(session)

    stale = next(row for row in rows if row["ticker"] == "PHASE3D-STALE")
    expired = next(row for row in rows if row["ticker"] == "PHASE3D-EXPIRED")
    assert stale["data_quality"] == "Stale market data"
    assert stale["snapshot_repair_status"] == "Refresh required"
    assert stale["recommended_action"] == "Reconnect or refresh snapshot"
    assert expired["data_quality"] == "Expired market"
    assert expired["snapshot_repair_status"] == "Not active"
    assert expired["recommended_action"] == "Exclude expired market"


def test_market_monitor_groups_and_deprioritizes_missing_multileg_sports_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        insert_market_snapshot(
            session,
            {
                "ticker": "PHASE3D-USABLE",
                "status": "open",
                "title": "Will the usable market data test resolve yes?",
                "series_ticker": "KXTEST",
                "event_ticker": "KXTEST-EVENT",
                "close_time": (now + timedelta(hours=2)).isoformat(),
                "liquidity_dollars": "750",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.31", "20"]],
                    "no_dollars": [["0.61", "20"]],
                }
            },
            now,
        )
        insert_market_ranking(
            session,
            {
                "ticker": "PHASE3D-USABLE",
                "ranked_at": now,
                "title": "Will the usable market data test resolve yes?",
                "status": "open",
                "forecast_model": "ensemble_v2",
                "forecast_probability": "0.55",
                "best_side": "BUY_YES",
                "best_price": "0",
                "midpoint": "0",
                "estimated_edge": "0.10",
                "liquidity_score": "65",
                "spread_score": "75",
                "time_score": "80",
                "model_confidence_score": "72",
                "opportunity_score": "45",
                "spread": "0",
                "liquidity": "0",
                "time_to_close_minutes": "120",
                "reason": "Seeded usable market data opportunity.",
            },
        )
        insert_market_snapshot(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-PHASE3D",
                "status": "open",
                "title": "yes Team A,yes Team B,yes Team C",
                "series_ticker": "KXSPORTS",
                "event_ticker": "KXSPORTS-EVENT",
                "close_time": (now + timedelta(hours=2)).isoformat(),
            },
            None,
            now,
        )
        insert_market_ranking(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-PHASE3D",
                "ranked_at": now,
                "title": "yes Team A,yes Team B,yes Team C",
                "status": "open",
                "series_ticker": "KXSPORTS",
                "event_ticker": "KXSPORTS-EVENT",
                "forecast_model": "ensemble_v2",
                "forecast_probability": "0.66",
                "best_side": "BUY_YES",
                "best_price": "0",
                "midpoint": "0",
                "estimated_edge": "0.18",
                "liquidity_score": "0",
                "spread_score": "0",
                "time_score": "80",
                "model_confidence_score": "82",
                "opportunity_score": "99",
                "spread": "0",
                "liquidity": "0",
                "time_to_close_minutes": "120",
                "reason": "Seeded multi-leg sports market with missing data.",
            },
        )
        for index, text in enumerate(("yes Team A", "yes Team B", "yes Team C")):
            session.add(
                MarketLeg(
                    ticker="KXMVESPORTSMULTIGAMEEXTENDED-PHASE3D",
                    leg_index=index,
                    parsed_at=now,
                    side="yes",
                    category="sports",
                    market_type="multi_leg",
                    entity_name=text.replace("yes ", ""),
                    operator="unknown",
                    threshold_value=None,
                    unit=None,
                    confidence="0.90",
                    raw_text=text,
                    reason="test leg",
                    raw_json="{}",
                )
            )
        session.flush()
        rows = market_monitor_rows(session, limit=2)

    assert rows[0]["ticker"] == "PHASE3D-USABLE"
    assert rows[0]["data_quality"] == "Usable market data"
    grouped = next(row for row in rows if row["ticker"] == "GROUPED-MISSING-SPORTS-MULTILEG")
    assert grouped["market"].startswith("1 multi-leg sports markets missing price/liquidity data.")
    assert "Team A; Team B; Team C" in grouped["market"]
    assert grouped["data_quality"] == "Missing market data"
    assert grouped["recommended_action"] == "Collect fresh snapshots before ranking"


def test_market_monitor_batches_related_market_reads(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        for ticker_index in range(40):
            for ranking_index in range(3):
                insert_market_ranking(
                    session,
                    {
                        "ticker": f"PHASE3D-BATCH-{ticker_index}",
                        "ranked_at": now - timedelta(minutes=ranking_index),
                        "title": f"Will batch market {ticker_index} resolve yes?",
                        "status": "open",
                        "forecast_model": "ensemble_v2",
                        "forecast_probability": "0.66",
                        "best_side": "BUY_YES",
                        "best_price": "0.40",
                        "estimated_edge": "0.18",
                        "liquidity_score": "85",
                        "spread_score": "90",
                        "time_score": "80",
                        "model_confidence_score": "82",
                        "opportunity_score": "88",
                        "spread": "0.05",
                        "liquidity": "1000",
                        "time_to_close_minutes": "120",
                        "reason": "Seeded batch-query regression ranking.",
                    },
                )
        session.flush()

        statements: list[str] = []

        def count_statements(*args) -> None:  # noqa: ANN002
            statements.append(args[2])

        event.listen(session.bind, "before_cursor_execute", count_statements)
        try:
            rows = market_monitor_rows(session, limit=20)
        finally:
            event.remove(session.bind, "before_cursor_execute", count_statements)

    assert len(rows) == 20
    assert len(statements) <= 4


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3d.db'}")
    return get_session_factory(engine)


def _seed_position_market(session) -> None:
    now = utc_now()
    snapshot = insert_market_snapshot(
        session,
        {
            "ticker": "PHASE3D-TEST",
            "status": "open",
            "title": "Will the Phase 3D workstation test market resolve yes?",
            "series_ticker": "KXSPORTS",
            "event_ticker": "KXSPORTS-EVENT",
            "close_time": (now + timedelta(hours=3)).isoformat(),
            "volume_fp": "1000",
            "open_interest_fp": "500",
            "liquidity_dollars": "12000",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.42", "20"]],
                "no_dollars": [["0.48", "20"]],
            }
        },
        now,
    )
    insert_forecast(
        session,
        ForecastOutput(
            ticker="PHASE3D-TEST",
            forecasted_at=now,
            model_name="ensemble_v2",
            yes_probability=Decimal("0.66"),
            market_mid_probability=None,
            best_yes_bid=Decimal("0.42"),
            best_yes_ask=Decimal(snapshot.best_yes_ask),
            feature_json={"source": "phase3d_test"},
        ),
    )
    insert_market_ranking(
        session,
        {
            "ticker": "PHASE3D-TEST",
            "ranked_at": now,
            "title": "Will the Phase 3D workstation test market resolve yes?",
            "status": "open",
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
            "spread": "0.06",
            "liquidity": "12000",
            "time_to_close_minutes": "180",
            "reason": "Seeded workstation opportunity.",
        },
    )
    upsert_position(
        session,
        ticker="PHASE3D-TEST",
        yes_contracts=3,
        no_contracts=0,
        avg_yes_price=Decimal("0.45"),
        avg_no_price=None,
        realized_pnl=Decimal("0"),
    )
    session.flush()
