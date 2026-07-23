from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    CryptoMarketLink,
    Feature,
    Forecast,
    Market,
    MarketLeg,
    MarketOpportunity,
    MarketRanking,
    MarketSnapshot,
    PaperFill,
    PaperOrder,
    PaperPnl,
    Settlement,
    SportsMarketLink,
)
from kalshi_predictor.roadmap.artifacts import verify_signed_artifact
from kalshi_predictor.roadmap.runtime_reports import (
    build_paper_settlement_throughput,
    build_runtime_category_census,
    write_runtime_roadmap_reports,
)


def _market(ticker: str, now: datetime) -> Market:
    return Market(
        ticker=ticker,
        title=ticker,
        status="open",
        raw_json="{}",
        first_seen_at=now,
        last_seen_at=now,
    )


def _leg(ticker: str, category: str, now: datetime) -> MarketLeg:
    return MarketLeg(
        ticker=ticker,
        leg_index=0,
        parsed_at=now,
        side="yes",
        category=category,
        market_type="binary",
        operator="equals",
        confidence="1",
        raw_text=ticker,
        reason="fixture",
        raw_json="{}",
    )


def test_runtime_census_reads_real_stage_tables_and_sports_provenance() -> None:
    now = datetime.now(UTC)
    engine = init_db("sqlite:///:memory:")
    with get_session_factory(engine)() as session:
        session.add_all([_market("BTC-1", now), _market("GAME-1", now)])
        session.add_all([_leg("BTC-1", "crypto", now), _leg("GAME-1", "sports", now)])
        session.add(
            CryptoMarketLink(
                ticker="BTC-1",
                symbol="BTC",
                detected_at=now,
                confidence="1",
                reason="fixture",
                raw_json="{}",
            )
        )
        session.add(
            SportsMarketLink(
                created_at=now,
                ticker="GAME-1",
                league="NFL",
                game_key="derived",
                market_type="binary",
                link_confidence="1",
                link_reason="market-derived",
                matched_terms_json="[]",
                raw_json='{"source":"market-derived-fallback"}',
            )
        )
        session.add(MarketSnapshot(ticker="BTC-1", captured_at=now, raw_market_json="{}"))
        session.add(
            Feature(
                ticker="BTC-1",
                feature_set_name="crypto",
                generated_at=now,
                source_timestamp=now,
                features_json="{}",
                created_at=now,
            )
        )
        forecast = Forecast(
            ticker="BTC-1",
            forecasted_at=now,
            model_name="crypto_v1",
            yes_probability="0.6",
            feature_json="{}",
        )
        session.add(forecast)
        session.flush()
        session.add(
            MarketRanking(
                ticker="BTC-1",
                ranked_at=now,
                forecast_model="crypto_v1",
                liquidity_score="1",
                spread_score="1",
                time_score="1",
                model_confidence_score="1",
                opportunity_score="1",
                reason="fixture",
                raw_json="{}",
            )
        )
        session.add(
            MarketOpportunity(
                ticker="BTC-1",
                detected_at=now,
                model_name="crypto_v1",
                side="yes",
                price="0.5",
                forecast_probability="0.6",
                estimated_edge="0.1",
                opportunity_score="1",
                status="open",
                reason="fixture",
                raw_json="{}",
            )
        )
        session.add(
            AdvancedRiskDecisionLog(
                decision_timestamp=now,
                created_at=now,
                version="1",
                mode="paper",
                action="allow",
                ticker="BTC-1",
                trade_intent_id="intent-1",
                phase_3m_tier="one",
                phase_3m_proposed_contracts=1,
                live_candidate_contracts=0,
                executed_contracts=0,
                risk_per_contract="0.5",
                planned_trade_risk="0.5",
                raw_caps_json="{}",
                bucketed_caps_json="{}",
                limiting_factors_json="[]",
                hard_blocks_json="[]",
                reason_codes_json="[]",
                raw_json="{}",
            )
        )
        session.commit()
        before = session.scalar(select(func.count()).select_from(MarketSnapshot))
        report = build_runtime_category_census(session, generated_at=now)
        after = session.scalar(select(func.count()).select_from(MarketSnapshot))

    crypto = report["categories"][0]
    sports = report["categories"][2]
    assert crypto["stage_coverage"]["verified_link"]["numerator"] == 1
    assert crypto["stage_coverage"]["risk_evidence"]["numerator"] == 1
    assert sports["stage_coverage"]["verified_link"]["numerator"] == 0
    assert report["runtime_evidence"]["database_read_only"] is True
    assert before == after == 1


def test_runtime_census_is_bounded_to_current_candidate_tickers() -> None:
    now = datetime.now(UTC)
    engine = init_db("sqlite:///:memory:")
    with get_session_factory(engine)() as session:
        session.add_all([_market("BTC-CURRENT", now), _market("BTC-OTHER", now)])
        session.add_all([_leg("BTC-CURRENT", "crypto", now), _leg("BTC-OTHER", "crypto", now)])
        session.commit()

        report = build_runtime_category_census(
            session,
            generated_at=now,
            market_limit=40,
            ticker_scope=["BTC-CURRENT"],
        )

    crypto = report["categories"][0]
    assert crypto["stage_coverage"]["verified_link"]["denominator"] == 1
    assert report["runtime_evidence"]["active_markets_scanned"] == 1
    assert report["runtime_evidence"]["ticker_scope"] == ["BTC-CURRENT"]
    assert report["runtime_evidence"]["ticker_scope_provided"] is True


def test_paper_throughput_reports_category_progress_pending_and_lineage() -> None:
    now = datetime.now(UTC)
    engine = init_db("sqlite:///:memory:")
    with get_session_factory(engine)() as session:
        btc_market = _market("BTC-1", now)
        weather_market = _market("WX-1", now)
        weather_market.close_time = now + timedelta(hours=1)
        session.add_all([btc_market, weather_market])
        session.add_all([_leg("BTC-1", "crypto", now), _leg("WX-1", "weather", now)])
        forecast = Forecast(
            ticker="BTC-1",
            forecasted_at=now,
            model_name="crypto_v1",
            yes_probability="0.6",
            feature_json="{}",
        )
        session.add(forecast)
        session.flush()
        settled = PaperOrder(
            ticker="BTC-1",
            forecast_id=forecast.id,
            created_at=now - timedelta(days=1),
            model_name="crypto_v1",
            side="yes",
            probability="0.6",
            market_price="0.5",
            limit_price="0.5",
            edge="0.1",
            quantity=1,
            status="filled",
            reason="fixture",
            raw_decision_json="{}",
        )
        pending = PaperOrder(
            ticker="WX-1",
            forecast_id=None,
            created_at=now,
            model_name="weather_v1",
            side="yes",
            probability="0.6",
            market_price="0.5",
            limit_price="0.5",
            edge="0.1",
            quantity=1,
            status="filled",
            reason="fixture",
            raw_decision_json="{}",
        )
        session.add_all([settled, pending])
        session.flush()
        session.add_all(
            [
                PaperFill(
                    paper_order_id=settled.id,
                    ticker="BTC-1",
                    filled_at=now,
                    side="yes",
                    price="0.5",
                    quantity=1,
                    fee="0",
                    raw_fill_json="{}",
                ),
                Settlement(
                    ticker="BTC-1",
                    settled_at=now,
                    result="yes",
                    yes_settlement_value="1",
                    raw_json="{}",
                    updated_at=now,
                ),
                PaperPnl(
                    ticker="BTC-1",
                    calculated_at=now,
                    yes_contracts=1,
                    no_contracts=0,
                    avg_yes_price="0.5",
                    settlement_result="yes",
                    realized_pnl="0.5",
                    unrealized_pnl="0",
                    total_pnl="0.5",
                    notes="settled market realized paper p&l",
                ),
            ]
        )
        session.commit()
        report = build_paper_settlement_throughput(session, generated_at=now)

    assert report["summary"]["settled"] == 1
    assert report["summary"]["awaiting_settlement"] == 1
    assert report["summary"]["filled"] == 2
    assert report["summary"]["past_market_close"] == 0
    assert report["categories"]["crypto"]["settled"] == 1
    assert report["categories"]["weather"]["awaiting_settlement"] == 1
    assert report["live_category_progress"]["crypto"]["remaining"] == 29
    assert report["lineage_gaps"] == [
        {
            "paper_order_id": pending.id,
            "ticker": "WX-1",
            "category": "weather",
            "gaps": ["FORECAST_ID_MISSING", "FILL_LINEAGE_MISSING", "SETTLEMENT_MISSING"],
        }
    ]
    assert report["pending_settlements"] == [
        {
            "paper_order_id": pending.id,
            "ticker": "WX-1",
            "category": "weather",
            "created_at": now.isoformat(),
            "age_hours": 0.0,
            "market_close_time": (now + timedelta(hours=1)).isoformat(),
            "past_market_close": False,
            "fill_rows": 0,
        }
    ]
    assert report["next_actions"][0]["code"] == "REPAIR_LINEAGE"
    assert all(value is False for value in report["safety"].values())


def test_zero_order_report_has_deterministic_reason() -> None:
    engine = init_db("sqlite:///:memory:")
    with get_session_factory(engine)() as session:
        report = build_paper_settlement_throughput(session)
    assert report["zero_trade_reasons"] == {"NO_PAPER_ORDERS": 1}
    assert report["summary"]["overall_remaining"] == 100
    assert report["next_actions"][0]["code"] == "DIAGNOSE_NO_ORDERS"


def test_paper_throughput_reads_only_bounded_recent_orders() -> None:
    now = datetime.now(UTC)
    engine = init_db("sqlite:///:memory:")
    with get_session_factory(engine)() as session:
        for index in range(3):
            session.add(
                PaperOrder(
                    ticker=f"BTC-{index}",
                    forecast_id=None,
                    created_at=now + timedelta(minutes=index),
                    model_name="crypto_v1",
                    side="yes",
                    probability="0.6",
                    market_price="0.5",
                    limit_price="0.5",
                    edge="0.1",
                    quantity=1,
                    status="open",
                    reason="fixture",
                    raw_decision_json="{}",
                )
            )
        session.commit()

        report = build_paper_settlement_throughput(
            session,
            generated_at=now + timedelta(minutes=3),
            order_limit=2,
        )

    assert report["summary"]["orders"] == 2
    assert report["summary"]["open"] == 2
    assert report["runtime_evidence"] == {
        "database_read_only": True,
        "order_limit": 2,
        "orders_truncated": True,
    }


def test_rejection_breakdown_has_denominators_and_safe_action() -> None:
    now = datetime.now(UTC)
    engine = init_db("sqlite:///:memory:")
    with get_session_factory(engine)() as session:
        session.add(_market("BTC-1", now))
        session.add(
            PaperOrder(
                ticker="BTC-1",
                forecast_id=None,
                created_at=now,
                model_name="crypto_v1",
                side="yes",
                probability="0.6",
                market_price="0.5",
                limit_price="0.5",
                edge="0.1",
                quantity=1,
                status="rejected",
                reason="insufficient edge",
                raw_decision_json="{}",
            )
        )
        session.commit()
        report = build_paper_settlement_throughput(session, generated_at=now)

    assert report["summary"]["rejected"] == 1
    assert report["rejection_breakdown"] == [
        {
            "reason": "INSUFFICIENT_EDGE",
            "count": 1,
            "denominator": 1,
            "rate": 1.0,
            "recommended_action": "Wait for genuine edge; do not lower thresholds.",
        }
    ]
    assert report["next_actions"][-1]["code"] == "ADDRESS_PRIMARY_REJECTION"


def test_open_order_is_not_misclassified_as_rejected() -> None:
    now = datetime.now(UTC)
    engine = init_db("sqlite:///:memory:")
    with get_session_factory(engine)() as session:
        session.add(_market("BTC-OPEN", now))
        session.add(
            PaperOrder(
                ticker="BTC-OPEN",
                forecast_id=None,
                created_at=now,
                model_name="crypto_v1",
                side="yes",
                probability="0.6",
                market_price="0.5",
                limit_price="0.5",
                edge="0.1",
                quantity=1,
                status="OPEN",
                reason="awaiting executable fill",
                raw_decision_json="{}",
            )
        )
        session.commit()
        report = build_paper_settlement_throughput(session, generated_at=now)

    assert report["summary"]["open"] == 1
    assert report["summary"]["rejected"] == 0
    assert report["rejection_breakdown"] == []
    assert report["zero_trade_reasons"] == {"OPEN_PAPER_ORDERS": 1}
    assert report["next_actions"][-1]["code"] == "INSPECT_OPEN_ORDERS"


def test_runtime_report_writer_emits_verified_artifacts(tmp_path) -> None:
    engine = init_db("sqlite:///:memory:")
    with get_session_factory(engine)() as session:
        paths = write_runtime_roadmap_reports(session, reports_root=tmp_path)
    assert set(paths) == {"category_census", "paper_throughput"}
    assert all(verify_signed_artifact(path)["verified"] for path in paths.values())
