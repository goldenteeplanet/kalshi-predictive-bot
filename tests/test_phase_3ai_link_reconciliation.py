from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.market_legs import link_coverage_dashboard, parse_and_store_market_legs
from kalshi_predictor.phase3ai import build_phase3ai_reconciliation, write_phase3ai_report
from kalshi_predictor.sports.repository import (
    insert_sports_market_link,
    upsert_sports_game,
    upsert_sports_team,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3ai_separates_partial_legs_markets_and_link_rows(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "SPORTS-MULTILEG-PARTIAL",
                "title": "yes Cal Raleigh: 1+,yes Kyle Schwarber: 1+",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            },
        )
        parse_and_store_market_legs(session, refresh=True)
        _seed_partial_link(session, "SPORTS-MULTILEG-PARTIAL", suffix="a")
        _seed_partial_link(session, "SPORTS-MULTILEG-PARTIAL", suffix="b")

        dashboard = link_coverage_dashboard(session)
        payload = build_phase3ai_reconciliation(session, upgrade_sports=False)

    sports = next(row for row in dashboard["category_rows"] if row["category"] == "sports")
    assert sports["partial_markets"] == 1
    assert sports["partial_legs"] == 2
    assert sports["partial_link_rows"] == 2
    assert payload["after"]["sports_reconciliation"]["partial_link_rows"] == 2
    assert payload["after"]["sports_reconciliation"]["unresolved_partial_markets"] == 1
    assert all(check["status"] == "PASS" for check in payload["consistency_checks"])


def test_phase3ai_upgrades_sports_partial_with_verified_schedule(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_dodgers_market(session)
        _seed_partial_link(session, "SPORTS-DODGERS-P3AI")
        _seed_verified_game(session)

        payload = build_phase3ai_reconciliation(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.40")),
            upgrade_sports=True,
        )
        session.commit()

    assert payload["sports_upgrade"]["verified_links_created"] == 1
    assert payload["after"]["sports_reconciliation"]["verified_schedule_markets"] == 1
    assert payload["after"]["sports_reconciliation"]["unresolved_partial_markets"] == 0
    assert payload["recommended_next_action"].startswith("Sports link counts reconcile")


def test_phase3ai_emits_sports_upgrade_progress(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    events = []
    with session_factory() as session:
        _seed_dodgers_market(session)
        _seed_partial_link(session, "SPORTS-DODGERS-P3AI")
        _seed_verified_game(session)

        build_phase3ai_reconciliation(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.40")),
            upgrade_sports=True,
            progress_callback=events.append,
            progress_every=1,
        )

    assert [event["status"] for event in events][0] == "START"
    assert [event["status"] for event in events][-1] == "DONE"
    assert any(event["processed"] == 1 for event in events)
    assert any(event["upgraded"] == 1 for event in events)


def test_phase3ai_report_renders(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase3ai"
    with session_factory() as session:
        upsert_market(
            session,
            {
                "ticker": "SPORTS-PARTIAL-REPORT",
                "title": "yes Cal Raleigh: 1+",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
            },
        )
        parse_and_store_market_legs(session, refresh=True)
        _seed_partial_link(session, "SPORTS-PARTIAL-REPORT")
        artifacts = write_phase3ai_report(
            session,
            output_dir=output_dir,
            upgrade_sports=False,
        )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")
    assert "Phase 3AI" in markdown
    assert "partial_link_rows" in markdown
    assert "Verified Sports Upgrade" in markdown


def test_phase3ai_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3ai-link-reconciliation", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output
    assert "progress-every" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ai.db'}")
    return get_session_factory(engine)


def _seed_dodgers_market(session) -> None:
    now = utc_now()
    upsert_market(
        session,
        {
            "ticker": "SPORTS-DODGERS-P3AI",
            "status": "open",
            "title": "Will the Dodgers beat the Yankees in tonight's MLB game?",
            "series_ticker": "KXMLB",
            "event_ticker": "KXMLB-DODGERS-YANKEES",
            "close_time": (now + timedelta(hours=6)).isoformat(),
        },
    )


def _seed_partial_link(session, ticker: str, *, suffix: str = "single") -> None:
    insert_sports_market_link(
        session,
        ticker=ticker,
        league="MLB",
        game_key=f"MLB:market-derived:{ticker.lower()}:{suffix}",
        market_type="PLAYER_PROP" if "MULTILEG" in ticker or "REPORT" in ticker else "MONEYLINE",
        link_confidence=Decimal("0.50"),
        link_reason="Market-derived fallback link.",
        matched_terms=["mlb", "market_derived"],
        raw_json={"source": "market-derived-fallback"},
    )


def _seed_verified_game(session) -> None:
    now = utc_now()
    upsert_sports_team(
        session,
        {"team_key": "LAD", "team_name": "Los Angeles Dodgers", "abbreviation": "LAD"},
        league="MLB",
    )
    upsert_sports_team(
        session,
        {"team_key": "NYY", "team_name": "New York Yankees", "abbreviation": "NYY"},
        league="MLB",
    )
    upsert_sports_game(
        session,
        {
            "game_key": "MLB:dodgers-yankees-p3ai",
            "scheduled_at": (now + timedelta(hours=6)).isoformat(),
            "home_team_key": "LAD",
            "away_team_key": "NYY",
            "status": "scheduled",
            "venue": "Dodger Stadium",
        },
        league="MLB",
    )
