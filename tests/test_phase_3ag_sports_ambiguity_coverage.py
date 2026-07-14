import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketLeg
from kalshi_predictor.phase3ag import write_phase3ag_repair_report, write_phase3ag_report
from kalshi_predictor.sports.repository import (
    insert_sports_market_link,
    upsert_sports_game,
    upsert_sports_team,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3ag_reports_soccer_gap_and_writes_manual_template(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        soccer_market = _seed_soccer_partial(session)
        _seed_soccer_leg(session, soccer_market.ticker, "Brazil")
        _seed_soccer_leg(session, soccer_market.ticker, "Bosnia and Herzegovina", index=1)
        _seed_suspect_verified_mlb_link(session)

        artifacts = write_phase3ag_report(
            session,
            output_dir=Path(tmp_path) / "reports",
            manual_template_path=Path(tmp_path) / "soccer_template.json",
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
        template = json.loads(artifacts.manual_template_path.read_text(encoding="utf-8"))

    assert artifacts.markdown_path.exists()
    assert payload["summary"]["soccer_partial_links"] == 1
    assert payload["summary"]["verified_soccer_games"] == 0
    assert payload["summary"]["suspect_verified_links"] == 1
    assert any(row["entity"] == "Brazil" for row in payload["soccer_coverage"]["top_entities"])
    assert template["source"] == "manual_verified_schedule_template"
    assert template["games"] == []


def test_phase3ag_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3ag-sports-ambiguity-coverage", "--help"])

    assert result.exit_code == 0
    assert "phase3ag-sports-ambiguity-coverage" in result.output


def test_phase3ag_repair_pass_groups_failures_and_alias_candidates(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    phase3ae_path = Path(tmp_path) / "phase3ae_failures.json"
    output_dir = Path(tmp_path) / "phase3ag_repair"
    with session_factory() as session:
        soccer_market = _seed_soccer_repair_market(session)
        wnba_market = _seed_wnba_window_gap_market(session)
        phase3ae_path.write_text(
            json.dumps(
                {
                    "rows": [
                        {
                            "ticker": soccer_market.ticker,
                            "status": "NO_VERIFIED_MATCH",
                            "league": "SOCCER",
                            "market_type": "MONEYLINE",
                            "partial_game_key": (
                                f"SOCCER:market-derived:{soccer_market.ticker.lower()}"
                            ),
                            "reason": "Verified schedules exist, but no match cleared.",
                        },
                        {
                            "ticker": wnba_market.ticker,
                            "status": "NO_VERIFIED_MATCH",
                            "league": "WNBA",
                            "market_type": "MONEYLINE",
                            "partial_game_key": (
                                f"WNBA:market-derived:{wnba_market.ticker.lower()}"
                            ),
                            "reason": "Verified schedules exist, but no match cleared.",
                        },
                    ]
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        artifacts = write_phase3ag_repair_report(
            session,
            output_dir=output_dir,
            phase3ae_path=phase3ae_path,
        )
        payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))

    causes = {row["cause"]: row["count"] for row in payload["cause_breakdown"]}
    assert causes["MISSING_TEAM_OR_PLAYER_ALIAS"] == 1
    assert causes["NO_VERIFIED_GAMES_IN_SCHEDULE_WINDOW"] == 1
    assert payload["summary"]["auto_upgrades_created"] == 0
    assert any(
        row["entity"] == "Man City" for row in payload["missing_alias_candidates"]
    )
    assert artifacts.alias_candidates_path.exists()
    assert artifacts.manual_candidates_path.exists()


def test_phase3ag_repair_pass_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3ag-sports-link-repair-pass", "--help"])

    assert result.exit_code == 0
    assert "phase3ag-sports-link-repair-pass" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ag.db'}")
    return get_session_factory(engine)


def _seed_soccer_partial(session):
    now = utc_now()
    market = upsert_market(
        session,
        {
            "ticker": "SOCCER-PARTIAL-P3AG",
            "status": "open",
            "title": "yes Brazil,yes Bosnia and Herzegovina,no Over 2.5 goals scored",
            "series_ticker": "KXMVECROSSCATEGORY",
            "event_ticker": "KXMVECROSSCATEGORY-SOCCER",
            "close_time": (now + timedelta(days=3)).isoformat(),
        },
    )
    insert_sports_market_link(
        session,
        ticker=market.ticker,
        league="SOCCER",
        game_key="SOCCER:market-derived:soccer-partial-p3ag",
        market_type="TOTAL",
        link_confidence=Decimal("0.50"),
        link_reason="Market-derived fallback link.",
        matched_terms=["soccer", "market_derived"],
        raw_json={"source": "market-derived-fallback"},
    )
    return market


def _seed_soccer_leg(session, ticker: str, entity: str, *, index: int = 0) -> None:
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=index,
            parsed_at=utc_now(),
            side="YES",
            category="sports",
            market_type="TOTAL",
            entity_name=entity,
            operator="UNKNOWN",
            threshold_value=None,
            unit=None,
            confidence="0.80",
            raw_text=f"yes {entity}",
            reason="test soccer entity",
            raw_json="{}",
        )
    )


def _seed_suspect_verified_mlb_link(session) -> None:
    now = utc_now()
    market = upsert_market(
        session,
        {
            "ticker": "MLB-SUSPECT-P3AG",
            "status": "finalized",
            "title": "yes Boston,yes Dodgers wins by over 6.5 runs",
            "series_ticker": "KXMLB",
            "event_ticker": "KXMLB-SUSPECT",
            "close_time": now.isoformat(),
        },
    )
    upsert_sports_team(
        session,
        {"team_key": "BOS", "team_name": "Boston Red Sox", "abbreviation": "BOS"},
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
            "game_key": "MLB:espn:mlb:suspect",
            "scheduled_at": (now + timedelta(days=2)).isoformat(),
            "home_team_key": "BOS",
            "away_team_key": "NYY",
            "status": "scheduled",
            "source": "espn_scoreboard",
        },
        league="MLB",
    )
    insert_sports_market_link(
        session,
        ticker=market.ticker,
        league="MLB",
        game_key="MLB:espn:mlb:suspect",
        market_type="TOTAL",
        link_confidence=Decimal("0.70"),
        link_reason="Phase 3AE verified schedule/team match.",
        matched_terms=["verified_schedule"],
        raw_json={"source": "verified_schedule"},
    )


def _seed_soccer_repair_market(session):
    now = utc_now()
    upsert_sports_team(
        session,
        {"team_key": "ARS", "team_name": "Arsenal", "abbreviation": "ARS"},
        league="SOCCER",
    )
    upsert_sports_team(
        session,
        {"team_key": "CHE", "team_name": "Chelsea", "abbreviation": "CHE"},
        league="SOCCER",
    )
    upsert_sports_game(
        session,
        {
            "game_key": "SOCCER:espn:eng.1:ars-che-p3ag",
            "scheduled_at": (now + timedelta(hours=3)).isoformat(),
            "home_team_key": "SOCCER:ars",
            "away_team_key": "SOCCER:che",
            "status": "scheduled",
            "source": "espn_scoreboard",
        },
        league="SOCCER",
    )
    market = upsert_market(
        session,
        {
            "ticker": "SOCCER-REPAIR-P3AG",
            "status": "open",
            "title": "Will Man City beat Arsenal in soccer?",
            "series_ticker": "KXSOCCER",
            "event_ticker": "KXSOCCER-MAN-CITY-ARSENAL",
            "close_time": (now + timedelta(hours=3)).isoformat(),
        },
    )
    _seed_soccer_leg(session, market.ticker, "Man City")
    _seed_soccer_leg(session, market.ticker, "Arsenal", index=1)
    insert_sports_market_link(
        session,
        ticker=market.ticker,
        league="SOCCER",
        game_key=f"SOCCER:market-derived:{market.ticker.lower()}",
        market_type="MONEYLINE",
        link_confidence=Decimal("0.50"),
        link_reason="Market-derived fallback link.",
        matched_terms=["soccer", "market_derived"],
        raw_json={"source": "market-derived-fallback"},
    )
    return market


def _seed_wnba_window_gap_market(session):
    now = utc_now()
    upsert_sports_team(
        session,
        {"team_key": "NYL", "team_name": "New York Liberty", "abbreviation": "NYL"},
        league="WNBA",
    )
    upsert_sports_team(
        session,
        {"team_key": "LVA", "team_name": "Las Vegas Aces", "abbreviation": "LVA"},
        league="WNBA",
    )
    upsert_sports_game(
        session,
        {
            "game_key": "WNBA:espn:wnba:liberty-aces-p3ag",
            "scheduled_at": (now + timedelta(days=3)).isoformat(),
            "home_team_key": "WNBA:nyl",
            "away_team_key": "WNBA:lva",
            "status": "scheduled",
            "source": "espn_scoreboard",
        },
        league="WNBA",
    )
    market = upsert_market(
        session,
        {
            "ticker": "WNBA-WINDOW-P3AG",
            "status": "open",
            "title": "Will the Liberty beat the Aces tonight?",
            "series_ticker": "KXWNBA",
            "event_ticker": "KXWNBA-LIBERTY-ACES",
            "close_time": now.isoformat(),
        },
    )
    _seed_soccer_leg(session, market.ticker, "Liberty")
    _seed_soccer_leg(session, market.ticker, "Aces", index=1)
    insert_sports_market_link(
        session,
        ticker=market.ticker,
        league="WNBA",
        game_key=f"WNBA:market-derived:{market.ticker.lower()}",
        market_type="MONEYLINE",
        link_confidence=Decimal("0.50"),
        link_reason="Market-derived fallback link.",
        matched_terms=["wnba", "market_derived"],
        raw_json={"source": "market-derived-fallback"},
    )
    return market
