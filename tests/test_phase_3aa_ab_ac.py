from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market, upsert_settlement
from kalshi_predictor.data.schema import MarketLeg, MarketRanking, PaperOrder
from kalshi_predictor.paper.models import ORDER_FILLED, PnlSummary
from kalshi_predictor.phase3aa import build_settlement_eta_schedule, run_paper_outcome_realizer
from kalshi_predictor.phase3ab import build_learning_governor, phase3ab_fast_settlement_settings
from kalshi_predictor.phase3ac import run_sports_provenance_repair
from kalshi_predictor.sports.repository import insert_sports_market_link
from kalshi_predictor.utils.time import utc_now


def test_phase3aa_realizes_only_exact_settlement_with_paper_jobs(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    calls = {"pnl": 0}

    def fake_pnl(_session, _settings):
        calls["pnl"] += 1
        return PnlSummary(
            positions_evaluated=1,
            pnl_rows_inserted=1,
            realized_pnl=Decimal("0.25"),
            unrealized_pnl=Decimal("0"),
            total_pnl=Decimal("0.25"),
        )

    with session_factory() as session:
        order = _paper_order("KXAA-EXACT")
        session.add(order)
        upsert_market(
            session,
            {
                "ticker": order.ticker,
                "status": "settled",
                "title": "Phase 3AA exact settlement",
            },
        )
        upsert_settlement(
            session,
            {
                "ticker": order.ticker,
                "result": "yes",
                "settlement_ts": "2026-06-24T12:00:00Z",
            },
        )

        payload = run_paper_outcome_realizer(
            session,
            settings=Settings(),
            dry_run=False,
            pnl_job=fake_pnl,
            confidence_job=lambda _session, _settings: _ConfidenceResult(),
            learning_metrics_job=lambda _session, _settings: {"settled": 1},
        )

    assert payload["eligible_after_sync"] == 1
    assert payload["pnl_realized"] is True
    assert payload["pnl_summary"]["pnl_rows_inserted"] == 1
    assert calls["pnl"] == 1


def test_phase3aa_eta_schedule_prioritizes_due_markets(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        order = _paper_order("KXAA-DUE")
        session.add(order)
        upsert_market(
            session,
            {
                "ticker": order.ticker,
                "status": "open",
                "title": "Phase 3AA due market",
                "close_time": utc_now(),
            },
        )

        payload = build_settlement_eta_schedule(session)

    assert payload["summary"]["active_unsettled"] == 1
    assert payload["summary"]["due_or_overdue"] == 1
    assert payload["recommended_watch_intervals"][0]["interval_minutes"] == 5


def test_phase3ab_learning_governor_routes_fast_and_slow_candidates(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(
            _ranking(
                "KXFAST-TEST",
                title="Bitcoin price today",
                minutes="120",
                score="50",
                edge="0.03",
            )
        )
        session.add(
            _ranking(
                "KXSLOW-SPORTS",
                title="Sports multi-game series winner",
                minutes="12000",
                score="60",
                edge="0.04",
            )
        )

        payload = build_learning_governor(session, settings=Settings(), limit=50)

    assert payload["summary"]["fast_settlement_candidates"] == 1
    assert payload["summary"]["slow_settlement_avoids"] == 1
    assert payload["recommended_env"]["EXECUTION_ENABLED"] == "false"
    tuned = phase3ab_fast_settlement_settings(Settings())
    assert tuned.learning_max_days_to_settlement == 1
    assert tuned.learning_prioritize_fast_settlement is True
    assert tuned.execution_enabled is False


def test_phase3ac_repairs_partial_sports_links_with_derived_provenance(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        ticker = "KXMLB-TEST"
        upsert_market(
            session,
            {
                "ticker": ticker,
                "title": "Will the Yankees beat the Red Sox?",
                "status": "open",
                "series_ticker": "KXMLB",
                "event_ticker": "KXMLB-TESTEVENT",
            },
        )
        session.add(
            MarketLeg(
                ticker=ticker,
                leg_index=0,
                parsed_at=utc_now(),
                side="YES",
                category="sports",
                market_type="MONEYLINE",
                entity_name="Yankees",
                operator="UNKNOWN",
                threshold_value=None,
                unit=None,
                confidence="0.80",
                raw_text="Yankees beat Red Sox",
                reason="test sports leg",
                raw_json="{}",
            )
        )
        insert_sports_market_link(
            session,
            ticker=ticker,
            league="MLB",
            game_key="MLB:market-derived:kxmlb-test",
            market_type="MONEYLINE",
            link_confidence=Decimal("0.50"),
            link_reason="market-derived fallback",
            matched_terms=["mlb", "market_derived"],
            raw_json={"source": "market-derived-fallback"},
        )

        payload = run_sports_provenance_repair(
            session,
            settings=Settings(),
            parse_first=False,
        )

    assert payload["before"]["partial_without_upgrade"] == 1
    assert payload["after"]["partial_without_upgrade"] == 0
    assert payload["after"]["provenance_counts"]["kalshi_event_derived"] >= 1
    assert payload["derived_schedule"]["links_created"] >= 1


def test_phase3aa_ab_ac_cli_help() -> None:
    runner = CliRunner()
    for command in (
        "phase3aa-realize",
        "phase3ab-learning-governor",
        "phase3ac-sports-provenance-repair",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3aa_ab_ac.db'}")
    return get_session_factory(engine)


def _paper_order(ticker: str) -> PaperOrder:
    return PaperOrder(
        ticker=ticker,
        forecast_id=None,
        created_at=utc_now(),
        model_name="ensemble_v2",
        side="BUY_YES",
        probability="0.55",
        market_price="0.50",
        limit_price="0.50",
        edge="0.05",
        quantity=1,
        status=ORDER_FILLED,
        reason="phase test",
        raw_decision_json="{}",
    )


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


@dataclass(frozen=True)
class _ConfidenceResult:
    scores_inserted: int = 1
    weights_inserted: int = 1
    rows: tuple[dict, ...] = ({"model": "ensemble_v2"},)
