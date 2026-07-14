import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.repository import insert_crypto_features
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import (
    encode_json,
    insert_forecast,
    insert_market_snapshot,
    upsert_market,
)
from kalshi_predictor.data.schema import LearningRejectionLog, MarketLeg, MarketRanking
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.learning.targets import generate_learning_targets
from kalshi_predictor.paper.strategy import generate_paper_decisions
from kalshi_predictor.phase3ak import (
    build_multi_leg_component_provenance,
    multi_leg_learning_eligibility,
)
from kalshi_predictor.phase3al import write_phase3al_report
from kalshi_predictor.phase3an import build_phase3an_crypto_feature_completeness
from kalshi_predictor.phase3aq import build_phase3aq_self_improvement_engine
from kalshi_predictor.sports.repository import (
    insert_sports_market_link,
    upsert_sports_game,
    upsert_sports_team,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3ak_marks_unsupported_multileg_and_blocks_learning(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = _seed_partial_multileg(session)

        payload = build_multi_leg_component_provenance(session, tickers=[ticker])
        gate = multi_leg_learning_eligibility(session, ticker)

    assert payload["summary"]["blocked_multi_leg_markets"] == 1
    assert payload["rows"][0]["learning_eligible"] is False
    assert payload["rows"][0]["component_status_counts"]["unsupported"] == 2
    assert gate["eligible"] is False
    assert gate["reason"] == "unsupported_component"


def test_phase3ak_allows_verified_multileg_with_usable_snapshot(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = _seed_verified_multileg(session)

        payload = build_multi_leg_component_provenance(session, tickers=[ticker])

    row = payload["rows"][0]
    assert row["learning_eligible"] is True
    assert row["component_status_counts"]["verified"] == 2
    assert row["snapshot_status"]["status"] == "usable_snapshot"


def test_learning_targets_reject_phase3ak_blocked_multileg(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = _seed_partial_multileg(session)
        _seed_ranking(session, ticker=ticker, title="yes Team A,yes Team B")

        result = generate_learning_targets(
            session,
            settings=Settings(learning_mode=True, learning_allowed_categories="sports"),
            limit=10,
        )
        rejection = session.scalar(select(LearningRejectionLog))

    assert result.inserted == 0
    assert rejection is not None
    assert rejection.reason == "multi_leg_component_not_verified"
    assert json.loads(rejection.raw_json)["phase3ak_gate"]["status"] == "INELIGIBLE"


def test_paper_strategy_blocks_phase3ak_multileg_even_with_edge(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = _seed_partial_multileg(session)
        snapshot = _seed_snapshot(session, ticker=ticker)
        _seed_forecast(session, snapshot.ticker, snapshot.captured_at)
        _seed_ranking(session, ticker=ticker, title="yes Team A,yes Team B")

        result = generate_paper_decisions(
            session,
            settings=Settings(learning_mode=True, paper_min_edge=Decimal("0.01")),
            model_name="ensemble_v2",
        )
        rejection = session.scalar(select(LearningRejectionLog))

    assert result.decisions == []
    assert result.skipped_due_to_risk_limits == 1
    assert rejection is not None
    assert rejection.reason == "multi_leg_component_not_verified"


def test_phase3al_report_and_phase3an_crypto_completeness_render(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        insert_crypto_features(
            session,
            symbol="BTC",
            source="coinbase",
            generated_at=utc_now(),
            window_minutes=60,
            features={"price": "100000", "trend_direction": "UP"},
            raw_json={"feature_version": "test"},
        )
        artifacts = write_phase3al_report(session, output_dir=Path(tmp_path) / "phase3al")
        crypto = build_phase3an_crypto_feature_completeness(
            session,
            symbols=["BTC", "ETH"],
        )

    assert artifacts.markdown_path.exists()
    assert crypto["summary"]["fresh_symbols"] == 1
    assert crypto["summary"]["missing_symbols"] == 1
    assert crypto["summary"]["can_rerun_crypto_v2"] is False


def test_phase3aq_self_improvement_is_advisory(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_phase3aq_self_improvement_engine(session, scan_limit=10)

    assert payload["advisory_policy"]["places_orders"] is False
    assert payload["advisory_policy"]["requires_human_approval"] is True
    assert payload["recommendations"]
    assert "Keep everything paper-only" in payload["next_build_prompt"]


def test_phase3ak_to_phase3aq_cli_help() -> None:
    runner = CliRunner()
    commands = (
        "phase3ak-multileg-provenance",
        "phase3al-learning-resume",
        "phase3am-sports-verified-upgrade",
        "phase3an-crypto-feature-completeness",
        "phase3ao-learning-reward-pipeline",
        "phase3ap-night-runner-v2",
        "phase3aq-self-improvement",
        "phase3aq-positive-ev-link-audit",
        "phase3aq-refresh-verified-opportunity-books",
        "phase3aq-settlement-check-split",
        "phase3aq-link-and-book-unblock-report",
        "phase3ar-crypto-forecast-coverage",
    )
    for command in commands:
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert command in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ak_to_3aq.db'}")
    return get_session_factory(engine)


def _seed_partial_multileg(session) -> str:
    ticker = "KXMVESPORTSMULTIGAMEEXTENDED-P3AK"
    market = upsert_market(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": "yes Team A,yes Team B",
            "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-EVENT",
            "close_time": (utc_now() + timedelta(hours=8)).isoformat(),
            "liquidity_dollars": "100",
        },
    )
    _seed_leg(session, market.ticker, "Team A", index=0)
    _seed_leg(session, market.ticker, "Team B", index=1)
    insert_sports_market_link(
        session,
        ticker=market.ticker,
        league="MLB",
        game_key=f"MLB:market-derived:{ticker.lower()}",
        market_type="MONEYLINE",
        link_confidence=Decimal("0.50"),
        link_reason="Market-derived fallback link.",
        matched_terms=["market_derived"],
        raw_json={"source": "market-derived-fallback"},
    )
    session.flush()
    return ticker


def _seed_verified_multileg(session) -> str:
    ticker = "KXMVESPORTSVERIFIED-P3AK"
    upsert_sports_team(
        session,
        {"team_key": "A", "team_name": "Team A", "abbreviation": "A"},
        league="MLB",
    )
    upsert_sports_team(
        session,
        {"team_key": "B", "team_name": "Team B", "abbreviation": "B"},
        league="MLB",
    )
    game, _ = upsert_sports_game(
        session,
        {
            "game_key": "GAME-A-B",
            "scheduled_at": (utc_now() + timedelta(hours=6)).isoformat(),
            "home_team_key": "MLB:a",
            "away_team_key": "MLB:b",
            "status": "scheduled",
        },
        league="MLB",
    )
    market = upsert_market(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": "yes Team A,yes Team B",
            "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            "event_ticker": "KXMVESPORTSVERIFIED-EVENT",
            "close_time": (utc_now() + timedelta(hours=6)).isoformat(),
            "liquidity_dollars": "100",
        },
    )
    _seed_leg(session, market.ticker, "Team A", index=0)
    _seed_leg(session, market.ticker, "Team B", index=1)
    _seed_snapshot(session, ticker=market.ticker)
    insert_sports_market_link(
        session,
        ticker=market.ticker,
        league="MLB",
        game_key=game.game_key,
        market_type="MONEYLINE",
        link_confidence=Decimal("0.95"),
        link_reason="Verified schedule/team match.",
        matched_terms=["team a", "team b"],
        raw_json={"source": "verified_schedule"},
    )
    session.flush()
    return ticker


def _seed_leg(session, ticker: str, entity: str, *, index: int) -> None:
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=index,
            parsed_at=utc_now(),
            side="YES",
            category="sports",
            market_type="MONEYLINE",
            entity_name=entity,
            operator="UNKNOWN",
            threshold_value=None,
            unit=None,
            confidence="0.90",
            raw_text=f"yes {entity}",
            reason="test sports leg",
            raw_json=encode_json({"entity": entity}),
        )
    )


def _seed_snapshot(session, *, ticker: str):
    captured = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": "yes Team A,yes Team B",
            "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.50",
            "last_price_dollars": "0.48",
            "liquidity_dollars": "100",
            "close_time": (captured + timedelta(hours=8)).isoformat(),
        },
        {"orderbook_fp": {"yes_dollars": [["0.45", "10"]], "no_dollars": [["0.50", "10"]]}},
        captured,
    )


def _seed_forecast(session, ticker: str, forecasted_at) -> None:
    insert_forecast(
        session,
        ForecastOutput(
            ticker=ticker,
            forecasted_at=forecasted_at,
            model_name="ensemble_v2",
            yes_probability=Decimal("0.70"),
            market_mid_probability=Decimal("0.50"),
            best_yes_bid=Decimal("0.45"),
            best_yes_ask=Decimal("0.50"),
            feature_json={"test": True},
            notes="test forecast",
        ),
    )


def _seed_ranking(session, *, ticker: str, title: str) -> None:
    session.add(
        MarketRanking(
            ticker=ticker,
            ranked_at=utc_now(),
            title=title,
            status="open",
            forecast_model="ensemble_v2",
            forecast_probability="0.70",
            best_side="BUY_YES",
            best_price="0.50",
            estimated_edge="0.20",
            liquidity="100",
            liquidity_score="80",
            spread="0.05",
            spread_score="90",
            time_to_close_minutes="480",
            time_score="75",
            model_confidence_score="70",
            opportunity_score="80",
            reason="test ranking",
            event_ticker="KXMVESPORTS",
            series_ticker="KXMVESPORTSMULTIGAMEEXTENDED",
            raw_json="{}",
        )
    )
    session.flush()
