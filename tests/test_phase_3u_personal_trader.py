from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    PersonalTraderRecommendationMemory,
    PositionSizingDecisionLog,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.institutional_dashboard.service import build_dashboard_snapshot
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.personal_trader.contracts import API_SCHEMA_VERSION, READ_ONLY_BOUNDARY
from kalshi_predictor.personal_trader.reports import generate_personal_trader_report
from kalshi_predictor.personal_trader.service import (
    build_personal_trade_brief,
    conversational_response,
    normalize_personal_trader_query,
    recommendation_audit_events,
)
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.routes import create_router
from kalshi_predictor.utils.time import utc_now


def test_phase_3u_normalizes_today_in_user_timezone() -> None:
    query = normalize_personal_trader_query(
        natural_language_query="What should I trade today?",
        settings=_settings(),
        requested_at=datetime(2026, 6, 23, 15, 0, tzinfo=UTC),
        as_of=datetime(2026, 6, 23, 15, 0, tzinfo=UTC),
        timezone="America/Chicago",
    )

    assert query.normalized_intent == "RANK_TODAYS_OPPORTUNITIES"
    assert query.resolved_day_start == datetime(2026, 6, 23, 5, 0, tzinfo=UTC)
    assert query.resolved_day_end == datetime(2026, 6, 24, 5, 0, tzinfo=UTC)
    assert query.maximum_recommendations == 3


def test_phase_3u_one_clear_eligible_trade_and_audit_memory(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_candidate(session_factory, "P3U-WEATHER", edge="0.18", event="P3U-EVENT-1")

    with session_factory() as session:
        brief = build_personal_trade_brief(session, settings=_settings(), persist=True)
        session.commit()
        events = recommendation_audit_events(session, brief_id=brief["brief_id"])

    assert brief["schema_version"] == "1.0.0"
    assert brief["summary"]["recommended_count"] == 1
    assert brief["no_trade"]["active"] is False
    assert brief["recommendations"][0]["market"]["market_ticker"] == "P3U-WEATHER"
    assert brief["recommendations"][0]["model_policy"]["phase_3m_proposed_quantity"] == 3
    assert brief["recommendations"][0]["model_policy"]["phase_3n_approved_quantity"] == 2
    assert brief["recommendations"][0]["economics"]["approved_quantity"] == 2
    assert "This is an advisory snapshot, not an order." in conversational_response(brief)
    assert len(events) >= 5
    assert any(row["event_type"] == "BRIEF_ISSUED" for row in events)


def test_phase_3u_trade_nothing_when_phase_3n_blocks(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_candidate(
        session_factory,
        "P3U-BLOCK",
        edge="0.20",
        event="P3U-EVENT-BLOCK",
        risk_action="BLOCK",
        risk_quantity=0,
    )

    with session_factory() as session:
        brief = build_personal_trade_brief(session, settings=_settings(), persist=False)

    assert brief["summary"]["recommended_count"] == 0
    assert brief["no_trade"]["active"] is True
    assert "PHASE_3N_BLOCK" in brief["no_trade"]["reason_codes"]
    assert "Phase 3N blocks new risk." == brief["no_trade"]["message"]


def test_phase_3u_ranking_is_stable_and_portfolio_aware(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_candidate(session_factory, "P3U-A", edge="0.18", event="P3U-SAME")
    _seed_candidate(session_factory, "P3U-B", edge="0.16", event="P3U-SAME")
    _seed_candidate(session_factory, "P3U-C", edge="0.14", event="P3U-OTHER")

    with session_factory() as session:
        first = build_personal_trade_brief(session, settings=_settings(), persist=False)
        second = build_personal_trade_brief(session, settings=_settings(), persist=False)

    first_tickers = [row["market"]["market_ticker"] for row in first["recommendations"]]
    second_tickers = [row["market"]["market_ticker"] for row in second["recommendations"]]

    assert first_tickers == second_tickers
    assert "P3U-A" in first_tickers
    assert "P3U-B" not in first_tickers
    assert any(
        "REDUNDANT_WITH_HIGHER_RANK" in row["reason_codes"]
        for row in first["watchlist"]
        if row["market_id"] == "P3U-B"
    )


def test_phase_3u_ui_api_and_report_are_read_only(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_candidate(session_factory, "P3U-UI", edge="0.18", event="P3U-UI-EVENT")
    client = TestClient(create_app(session_factory=session_factory, settings=_settings()))

    page = client.get("/personal-trader")
    query = client.post(
        "/personal-trader/query",
        json={
            "schema_version": API_SCHEMA_VERSION,
            "natural_language_query": "What should I trade today?",
        },
    )
    bad_schema = client.post("/personal-trader/query", json={"schema_version": "old"})

    assert page.status_code == 200
    assert "Personal AI Trader" in page.text
    assert "demo-execute" not in page.text
    assert "paper-trade" not in page.text
    assert query.status_code == 200
    payload = query.json()
    assert payload["schema_version"] == API_SCHEMA_VERSION
    assert payload["data"]["summary"]["recommended_count"] == 1
    assert bad_schema.status_code == 400
    assert bad_schema.json()["detail"]["code"] == "PERSONAL_TRADER_SCHEMA_UNSUPPORTED"

    with session_factory() as session:
        report = generate_personal_trader_report(
            session,
            output_path=Path(tmp_path) / "personal_trader.md",
            settings=_settings(),
        )
        memory_count = session.query(PersonalTraderRecommendationMemory).count()

    assert "No-Write Proof" in report.read_text(encoding="utf-8")
    assert memory_count >= 1


def test_phase_3u_routes_and_cli_smoke(tmp_path) -> None:
    router = create_router(session_factory=_session_factory(tmp_path), settings=_settings())
    personal_routes = [
        route
        for route in router.routes
        if getattr(route, "path", "").startswith("/personal-trader")
    ]

    for route in personal_routes:
        path = route.path
        assert "demo-execute" not in path
        assert "paper-trade" not in path

    runner = CliRunner()
    for command in (
        "personal-trader-status",
        "personal-trader-brief",
        "personal-trader-audit",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    get_settings.cache_clear()
    output = Path(tmp_path) / "personal_trader_brief.md"
    result = runner.invoke(
        app,
        [
            "personal-trader-brief",
            "--enable-advisory",
            "--output",
            str(output),
        ],
        env={"DATABASE_URL": f"sqlite:///{Path(tmp_path) / 'cli.db'}"},
    )
    get_settings.cache_clear()

    assert result.exit_code == 0
    assert output.exists()
    assert "Safety: advisory only" in result.output


def test_phase_3u_phase_3t_panel_and_no_write_boundary(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    _seed_candidate(session_factory, "P3U-T", edge="0.18", event="P3U-T-EVENT")

    with session_factory() as session:
        snapshot = build_dashboard_snapshot(session, settings=_settings())

    assert "personal_trader_brief" in snapshot["panels"]
    assert snapshot["panels"]["personal_trader_brief"]["read_only"] is True
    assert READ_ONLY_BOUNDARY["allow_order_create"] is False
    assert READ_ONLY_BOUNDARY["allow_live_execution"] is False


def _settings() -> Settings:
    return Settings(
        phase_3u_personal_ai_trader_enabled=True,
        phase_3u_mode="PAPER_ADVISORY",
        phase_3u_max_quote_age_seconds=7200,
        phase_3u_max_forecast_age_seconds=7200,
        phase_3u_max_opportunity_age_seconds=7200,
        phase_3u_max_risk_age_seconds=7200,
        phase_3t_institutional_dashboard_enabled=True,
        phase_3t_mode="read_only_shadow",
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3u.db'}")
    return get_session_factory(engine)


def _seed_candidate(
    session_factory,
    ticker: str,
    *,
    edge: str,
    event: str,
    risk_action: str = "ALLOW",
    risk_quantity: int = 2,
) -> None:
    with session_factory() as session:
        now = utc_now()
        close_time = now + timedelta(hours=3)
        snapshot = insert_market_snapshot(
            session,
            {
                "ticker": ticker,
                "status": "open",
                "title": f"Will {ticker} resolve yes for a weather benchmark?",
                "series_ticker": "KXWEATHER",
                "event_ticker": event,
                "close_time": close_time.isoformat(),
                "volume_fp": "1000",
                "open_interest_fp": "500",
                "liquidity_dollars": "12000",
                "rules_primary": "Settles from the official source.",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.42", "20"]],
                    "no_dollars": [["0.52", "20"]],
                }
            },
            now,
        )
        insert_forecast(
            session,
            ForecastOutput(
                ticker=ticker,
                forecasted_at=now,
                model_name="ensemble_v2",
                yes_probability=Decimal("0.66"),
                market_mid_probability=None,
                best_yes_bid=Decimal("0.42"),
                best_yes_ask=Decimal(snapshot.best_yes_ask),
                feature_json={"source": "phase3u_test"},
            ),
        )
        ranking = insert_market_ranking(
            session,
            {
                "ticker": ticker,
                "ranked_at": now,
                "title": f"Will {ticker} resolve yes for a weather benchmark?",
                "status": "open",
                "series_ticker": "KXWEATHER",
                "event_ticker": event,
                "forecast_model": "ensemble_v2",
                "market_probability": "0.48",
                "forecast_probability": "0.66",
                "best_side": "BUY_YES",
                "best_price": "0.48",
                "estimated_edge": edge,
                "liquidity_score": "85",
                "spread_score": "90",
                "time_score": "80",
                "model_confidence_score": "82",
                "opportunity_score": "88",
                "spread": "0.02",
                "time_to_close_minutes": "180",
                "reason": "Seeded Phase 3U test opportunity.",
            },
        )
        sizing = PositionSizingDecisionLog(
            decision_timestamp=now,
            created_at=now,
            version="3m-policy-v1",
            mode="shadow",
            strategy_id="phase3u-test",
            instrument=ticker,
            ticker=ticker,
            model_name="ensemble_v2",
            trade_intent_id=f"intent-{ticker}",
            order_correlation_id=f"corr-{ticker}",
            paper_order_id=None,
            tier="MEDIUM",
            composite_score="0.80",
            proposed_contracts=3,
            live_candidate_contracts=3,
            executed_contracts=3,
            factor_scores_json="{}",
            factor_weights_json="{}",
            adjusted_historical_accuracy="0.60",
            historical_sample_size=25,
            drawdown_utilization="0.10",
            caps_json="{}",
            limiting_factors_json="[]",
            reason_codes_json="[]",
            fallback_used=0,
            raw_json='{"source":"phase3u_test"}',
        )
        risk = AdvancedRiskDecisionLog(
            decision_timestamp=now,
            created_at=now,
            version="3n-policy-v1",
            mode="shadow",
            action=risk_action,
            strategy_id="phase3u-test",
            model_id="ensemble_v2",
            category_id="weather",
            instrument_id=ticker,
            correlation_group_id=event,
            ticker=ticker,
            trade_intent_id=f"intent-{ticker}",
            order_correlation_id=f"corr-{ticker}",
            position_sizing_decision_id=None,
            paper_order_id=None,
            reservation_id=None,
            phase_3m_tier="MEDIUM",
            phase_3m_proposed_contracts=3,
            live_candidate_contracts=risk_quantity,
            executed_contracts=risk_quantity,
            risk_per_contract="0.48",
            planned_trade_risk=str(Decimal("0.48") * Decimal(max(0, risk_quantity))),
            raw_caps_json="{}",
            bucketed_caps_json="{}",
            limiting_factors_json="[]",
            hard_blocks_json='["TEST_BLOCK"]' if risk_action == "BLOCK" else "[]",
            reason_codes_json='["TEST_BLOCK"]' if risk_action == "BLOCK" else "[]",
            fallback_used=0,
            raw_json=f'{{"ranking_id": {ranking.id}}}',
        )
        session.add_all([sizing, risk])
        session.commit()
