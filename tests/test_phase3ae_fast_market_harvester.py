from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketRanking
from kalshi_predictor.phase3ae_fast_market import (
    build_fast_market_harvester,
    write_phase3ae_fast_market_harvester_report,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3ae_fast_market_harvester_routes_ranked_and_unranked_fast_markets(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXFAST-RANKED",
                "title": "Bitcoin price today",
                "status": "open",
                "series_ticker": "KXBTC",
                "event_ticker": "KXBTC-FAST",
                "close_time": now + timedelta(hours=2),
            },
        )
        upsert_market(
            session,
            {
                "ticker": "KXFAST-UNRANKED",
                "title": "Ethereum price today",
                "status": "open",
                "series_ticker": "KXETH",
                "event_ticker": "KXETH-FAST",
                "close_time": now + timedelta(hours=4),
            },
        )
        session.add(
            _ranking(
                "KXFAST-RANKED",
                title="Bitcoin price today",
                minutes="120",
                score="60",
                edge="0.04",
            )
        )

        payload = build_fast_market_harvester(
            session,
            settings=Settings(),
            ranking_limit=50,
            market_limit=50,
        )

    summary = payload["summary"]
    assert summary["ranked_fast_settlement_candidates"] == 1
    assert summary["open_0_24h_markets_seen"] == 2
    assert summary["open_0_24h_markets_missing_current_ranking"] == 1
    assert summary["paper_trade_creation_allowed"] is False
    assert payload["live_or_demo_execution"] is False
    assert payload["order_submission"] is False
    assert payload["top_fast_ranked_candidates"][0]["ticker"] == "KXFAST-RANKED"
    assert payload["open_0_24h_markets_missing_current_ranking"][0]["ticker"] == (
        "KXFAST-UNRANKED"
    )
    assert any(
        "forecast --model ensemble_v2" in row["command"]
        for row in payload["recommended_commands"]
    )


def test_phase3ae_fast_market_harvester_flags_stale_ranking_time_bucket(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "KXFAST-STALE-RANKING",
                "title": "Weather high temperature today",
                "status": "open",
                "series_ticker": "KXWEATHER",
                "event_ticker": "KXWEATHER-FAST",
                "close_time": now + timedelta(hours=3),
            },
        )
        session.add(
            _ranking(
                "KXFAST-STALE-RANKING",
                title="Weather high temperature today",
                minutes="2880",
                score="50",
                edge="0.02",
            )
        )

        payload = build_fast_market_harvester(
            session,
            settings=Settings(),
            ranking_limit=50,
            market_limit=50,
        )

    stale_rows = payload["open_0_24h_markets_stale_or_missing_ranking"]
    assert stale_rows[0]["ticker"] == "KXFAST-STALE-RANKING"
    assert stale_rows[0]["harvest_gap"] == "RANKING_NOT_FAST_SETTLEMENT"
    assert payload["summary"]["open_0_24h_markets_stale_or_missing_ranking"] == 1


def test_phase3ae_fast_market_harvester_writer_outputs_artifacts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase3ae_fast_market_harvester_report(
            session,
            output_dir=Path(tmp_path) / "phase3ae_fast_market",
            settings=Settings(),
        )

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert "Phase 3AE Fast Market Harvester" in artifacts.markdown_path.read_text()


def test_phase3ae_fast_market_harvester_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3ae-fast-market-harvester", "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ae_fast_market.db'}")
    return get_session_factory(engine)


def _ranking(
    ticker: str,
    *,
    title: str,
    minutes: str,
    score: str,
    edge: str,
) -> MarketRanking:
    return MarketRanking(
        ticker=ticker,
        ranked_at=utc_now(),
        title=title,
        status="open",
        series_ticker=ticker.split("-", 1)[0],
        event_ticker=f"{ticker}-EVENT",
        volume="100",
        open_interest="100",
        liquidity="100",
        spread="0.02",
        midpoint="0.50",
        time_to_close_minutes=minutes,
        forecast_model="ensemble_v2",
        forecast_probability="0.55",
        best_side="YES",
        best_price="0.50",
        estimated_edge=edge,
        liquidity_score="60",
        spread_score="80",
        time_score="80",
        model_confidence_score="50",
        opportunity_score=score,
        reason="test ranking",
        raw_json="{}",
    )
