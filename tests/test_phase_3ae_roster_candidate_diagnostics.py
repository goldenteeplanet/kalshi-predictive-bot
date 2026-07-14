import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketLeg, SportsMarketLink
from kalshi_predictor.phase3ae_roster_candidates import (
    CLEAN_PHASE3AE_CANDIDATE,
    MARKET_TYPE_NOT_CLEAN,
    MIXED_SPORT_PLAYER_LEGS,
    MULTIPLE_CLEAN_GAME_CANDIDATES,
    NO_VERIFIED_ROSTER_PLAYER_MENTIONED,
    PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW,
    ROUND_PLACEHOLDER_GAME,
    build_phase3ae_roster_candidate_diagnostics,
    write_phase3ae_roster_candidate_diagnostics,
)
from kalshi_predictor.sports.repository import (
    insert_sports_market_link,
    upsert_sports_game,
    upsert_sports_team,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3ae_roster_diagnostics_identifies_clean_candidate_without_upgrade(
    tmp_path,
) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(session, title="Will Caitlin Clark score 20+ points tonight?")
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(
            session,
            game_key="WNBA:fever-sparks",
            home_key="IND",
            home_name="Indiana Fever",
            home_abbreviation="IND",
            away_key="LAS",
            away_name="Los Angeles Sparks",
            away_abbreviation="LAS",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
            rework_queue_path=None,
        )
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["clean_phase3ae_candidates"] == 1
    assert payload["rows"][0]["upgrade_candidate_status"] == CLEAN_PHASE3AE_CANDIDATE
    assert payload["rows"][0]["clean_candidate_games"][0]["game_key"] == "WNBA:fever-sparks"
    assert link_count == 1


def test_phase3ae_roster_diagnostics_blocks_wrong_team_player_prop(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(session, title="Will Caitlin Clark score 20+ points tonight?")
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(
            session,
            game_key="WNBA:sparks-liberty",
            home_key="LAS",
            home_name="Los Angeles Sparks",
            home_abbreviation="LAS",
            away_key="NYL",
            away_name="New York Liberty",
            away_abbreviation="NYL",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
            rework_queue_path=None,
        )
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["clean_phase3ae_candidates"] == 0
    assert payload["rows"][0]["upgrade_candidate_status"] == (
        PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW
    )
    assert PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW in payload["rows"][0]["rejection_reasons"]
    assert link_count == 1


def test_phase3ae_roster_diagnostics_reports_ambiguous_clean_games(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(session, title="Will Caitlin Clark score 20+ points tonight?")
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(
            session,
            game_key="WNBA:fever-sparks",
            home_key="IND",
            home_name="Indiana Fever",
            home_abbreviation="IND",
            away_key="LAS",
            away_name="Los Angeles Sparks",
            away_abbreviation="LAS",
        )
        _seed_verified_game(
            session,
            game_key="WNBA:fever-liberty",
            home_key="IND",
            home_name="Indiana Fever",
            home_abbreviation="IND",
            away_key="NYL",
            away_name="New York Liberty",
            away_abbreviation="NYL",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
        )
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["rows"][0]["upgrade_candidate_status"] == MULTIPLE_CLEAN_GAME_CANDIDATES
    assert payload["rows"][0]["clean_candidate_count"] == 2
    assert payload["summary"]["manual_disambiguation_rows"] == 1
    assert link_count == 1


def test_phase3ae_roster_diagnostics_reports_missing_roster_evidence(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(session, title="Will Courtney Williams score 20+ points tonight?")
        _seed_partial_link(session, market.ticker)
        _seed_market_leg(session, market.ticker, entity_name="Courtney Williams")
        _seed_verified_game(
            session,
            game_key="WNBA:lynx-sun",
            home_key="MIN",
            home_name="Minnesota Lynx",
            home_abbreviation="MIN",
            away_key="CON",
            away_name="Connecticut Sun",
            away_abbreviation="CON",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
        )

    assert payload["rows"][0]["upgrade_candidate_status"] == NO_VERIFIED_ROSTER_PLAYER_MENTIONED
    assert payload["top_missing_roster_players"][0]["player_name"] == "Courtney Williams"


def test_phase3ae_roster_diagnostics_separates_known_cross_sport_player_leg(
    tmp_path,
) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(
            session,
            title="Will Shohei Ohtani score a goal in Brazil vs Korea Republic?",
            series_ticker="KXSOCCER",
            event_ticker="KXSOCCER-BRA-KOR",
        )
        _seed_partial_link(session, market.ticker, league="SOCCER")
        _seed_market_leg(session, market.ticker, entity_name="Shohei Ohtani")
        _seed_verified_game(
            session,
            league="SOCCER",
            game_key="SOCCER:brazil-korea",
            home_key="BRA",
            home_name="Brazil",
            home_abbreviation="BRA",
            away_key="KOR",
            away_name="Korea Republic",
            away_abbreviation="KOR",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
            rework_queue_path=None,
        )

    row = payload["rows"][0]
    assert row["upgrade_candidate_status"] == MIXED_SPORT_PLAYER_LEGS
    assert MIXED_SPORT_PLAYER_LEGS in row["rejection_reasons"]
    assert NO_VERIFIED_ROSTER_PLAYER_MENTIONED not in row["rejection_reasons"]
    assert row["missing_roster_entities"] == []
    assert row["cross_sport_player_entities"] == [
        {
            "entity_name": "Shohei Ohtani",
            "inferred_league": "MLB",
            "target_league": "SOCCER",
        }
    ]
    assert payload["summary"]["mixed_sport_player_leg_rows"] == 1
    assert payload["top_missing_roster_players"] == []
    assert payload["top_cross_sport_player_leaks"][0]["player_name"] == "Shohei Ohtani"


def test_phase3ae_roster_diagnostics_uses_verified_other_league_roster_as_cross_sport(
    tmp_path,
) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(
            session,
            title="Will Caitlin Clark score a goal in Brazil vs Canada?",
            series_ticker="KXSOCCER",
            event_ticker="KXSOCCER-BRA-CAN",
        )
        _seed_partial_link(session, market.ticker, league="SOCCER")
        _seed_market_leg(session, market.ticker, entity_name="Caitlin Clark")
        _seed_verified_game(
            session,
            league="SOCCER",
            game_key="SOCCER:brazil-canada",
            home_key="BRA",
            home_name="Brazil",
            home_abbreviation="BRA",
            away_key="CAN",
            away_name="Canada",
            away_abbreviation="CAN",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
        )

    row = payload["rows"][0]
    assert row["upgrade_candidate_status"] == MIXED_SPORT_PLAYER_LEGS
    assert row["cross_sport_player_entities"][0]["inferred_league"] == "WNBA"
    assert row["missing_roster_entities"] == []


def test_phase3ae_roster_diagnostics_suppresses_unknown_legs_in_mixed_sport_rows(
    tmp_path,
) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(
            session,
            title="Will Shohei Ohtani and Trey Yesavage each score in Brazil vs Canada?",
            series_ticker="KXSOCCER",
            event_ticker="KXSOCCER-BRA-CAN",
        )
        _seed_partial_link(session, market.ticker, league="SOCCER")
        _seed_market_leg(session, market.ticker, entity_name="Shohei Ohtani")
        _seed_market_leg(session, market.ticker, entity_name="Trey Yesavage", leg_index=1)
        _seed_verified_game(
            session,
            league="SOCCER",
            game_key="SOCCER:brazil-canada",
            home_key="BRA",
            home_name="Brazil",
            home_abbreviation="BRA",
            away_key="CAN",
            away_name="Canada",
            away_abbreviation="CAN",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
            rework_queue_path=None,
        )

    row = payload["rows"][0]
    assert row["upgrade_candidate_status"] == MIXED_SPORT_PLAYER_LEGS
    assert NO_VERIFIED_ROSTER_PLAYER_MENTIONED not in row["rejection_reasons"]
    assert row["missing_roster_entities"] == ["Trey Yesavage"]
    assert payload["top_missing_roster_players"] == []


def test_phase3ae_roster_diagnostics_filters_cross_sport_rework_queue(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    rework_path = Path(tmp_path) / "phase3ah_roster_rework_queue.json"
    rework_path.write_text(
        json.dumps(
            [
                {"league": "SOCCER", "player_name": "Shohei Ohtani", "count": 5},
                {"league": "SOCCER", "player_name": "Amad Diallo", "count": 4},
                {"league": "WNBA", "player_name": "Courtney Williams", "count": 3},
                {"league": "WNBA", "player_name": "Pete Crow-Armstrong", "count": 2},
                {"league": "WNBA", "player_name": "Caitlin Clark", "count": 2},
                {"league": "WNBA", "player_name": "San Francisco", "count": 1},
                {"league": "SOCCER", "player_name": "A'ja Wilson", "count": 1},
            ]
        ),
        encoding="utf-8",
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
            rework_queue_path=rework_path,
        )

    missing_names = {row["player_name"] for row in payload["top_missing_roster_players"]}
    verify_names = {row["player_name"] for row in payload["next_20_rows_to_verify"]}
    assert missing_names == {"Amad Diallo", "Courtney Williams"}
    assert verify_names == {"Amad Diallo", "Courtney Williams"}


def test_phase3ae_roster_diagnostics_blocks_round_placeholder_games(tmp_path) -> None:
    evidence_path = _write_soccer_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(
            session,
            title="Will Vinicius Junior score a goal for Brazil?",
            series_ticker="KXSOCCER",
            event_ticker="KXSOCCER-BRA-RD16",
        )
        _seed_partial_link(session, market.ticker, league="SOCCER")
        _seed_market_leg(session, market.ticker, entity_name="Vinicius Junior")
        _seed_verified_game(
            session,
            league="SOCCER",
            game_key="SOCCER:rd16-w1-rd16-w2",
            home_key="RD16-W1",
            home_name="Round of 16 Winner 1",
            home_abbreviation="RD16W1",
            away_key="RD16-W2",
            away_name="Round of 16 Winner 2",
            away_abbreviation="RD16W2",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
        )

    row = payload["rows"][0]
    assert row["upgrade_candidate_status"] == ROUND_PLACEHOLDER_GAME
    assert ROUND_PLACEHOLDER_GAME in row["rejection_reasons"]
    assert row["clean_candidate_count"] == 0
    assert payload["summary"]["round_placeholder_game_rows"] == 1
    assert payload["top_round_placeholder_games"][0]["game_key"] == "SOCCER:rd16-w1-rd16-w2"


def test_phase3ae_roster_diagnostics_blocks_extra_unverified_player_leg(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(
            session,
            title="Will Caitlin Clark and Courtney Williams each score 20+ points tonight?",
        )
        _seed_partial_link(session, market.ticker)
        _seed_market_leg(session, market.ticker, entity_name="Caitlin Clark")
        _seed_market_leg(
            session,
            market.ticker,
            entity_name="Courtney Williams",
            leg_index=1,
        )
        _seed_verified_game(
            session,
            game_key="WNBA:fever-sparks",
            home_key="IND",
            home_name="Indiana Fever",
            home_abbreviation="IND",
            away_key="LAS",
            away_name="Los Angeles Sparks",
            away_abbreviation="LAS",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
        )
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["clean_phase3ae_candidates"] == 0
    assert payload["rows"][0]["upgrade_candidate_status"] == NO_VERIFIED_ROSTER_PLAYER_MENTIONED
    assert payload["rows"][0]["missing_roster_entities"] == ["Courtney Williams"]
    assert link_count == 1


def test_phase3ae_roster_diagnostics_blocks_extra_non_player_component_leg(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(
            session,
            title="Will Caitlin Clark score 20+ points tonight?",
        )
        _seed_partial_link(session, market.ticker)
        _seed_market_leg(session, market.ticker, entity_name="Caitlin Clark")
        _seed_market_leg(
            session,
            market.ticker,
            entity_name="points scored",
            leg_index=1,
            category="general",
            market_type="THRESHOLD",
        )
        _seed_verified_game(
            session,
            game_key="WNBA:fever-sparks",
            home_key="IND",
            home_name="Indiana Fever",
            home_abbreviation="IND",
            away_key="LAS",
            away_name="Los Angeles Sparks",
            away_abbreviation="LAS",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
        )

    row = payload["rows"][0]
    assert payload["summary"]["clean_phase3ae_candidates"] == 0
    assert row["upgrade_candidate_status"] == MARKET_TYPE_NOT_CLEAN
    assert MARKET_TYPE_NOT_CLEAN in row["rejection_reasons"]
    assert row["clean_candidate_count"] == 1
    assert row["unsupported_component_legs"] == [
        {
            "category": "general",
            "entity_name": "points scored",
            "market_type": "THRESHOLD",
            "raw_text": "points scored",
        }
    ]


def test_phase3ae_roster_diagnostics_suppresses_country_entity_player_prop_leak(
    tmp_path,
) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(
            session,
            title="yes Congo DR: 5+,yes Both Teams To Score",
            series_ticker="KXMVESPORTS",
            event_ticker="KXMVESPORTS-FIFA",
        )
        _seed_partial_link(session, market.ticker, league="SOCCER")
        _seed_market_leg(
            session,
            market.ticker,
            entity_name="Congo DR",
            category="sports",
            market_type="PLAYER_PROP",
        )
        _seed_market_leg(
            session,
            market.ticker,
            entity_name="Both Teams To Score",
            leg_index=1,
            category="general",
            market_type="BOTH_TEAMS_SCORE",
        )

        payload = build_phase3ae_roster_candidate_diagnostics(
            session,
            roster_evidence_path=evidence_path,
            rework_queue_path=None,
        )

    row = payload["rows"][0]
    assert row["upgrade_candidate_status"] == MARKET_TYPE_NOT_CLEAN
    assert row["missing_roster_entities"] == []
    assert row["suppressed_roster_entities"] == [
        {
            "entity_name": "Congo DR",
            "reason": "TEAM_OR_COMPETITION_ENTITY",
            "verified_entity_type": "TEAM_OR_COMPETITION_ENTITY",
        }
    ]
    assert {
        item["player_name"] for item in payload["top_missing_roster_players"]
    }.isdisjoint({"Congo DR"})


def test_phase3ae_roster_diagnostics_writer_outputs_all_artifacts(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_market(session, title="Will Caitlin Clark score 20+ points tonight?")
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(
            session,
            game_key="WNBA:fever-sparks",
            home_key="IND",
            home_name="Indiana Fever",
            home_abbreviation="IND",
            away_key="LAS",
            away_name="Los Angeles Sparks",
            away_abbreviation="LAS",
        )

        artifacts = write_phase3ae_roster_candidate_diagnostics(
            session,
            output_dir=Path(tmp_path) / "phase3ae_roster_candidates",
            roster_evidence_path=evidence_path,
        )

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.clean_candidates_path.exists()
    assert artifacts.blockers_path.exists()
    assert artifacts.manual_disambiguation_path.exists()


def test_phase3ae_roster_candidate_diagnostics_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3ae-roster-candidate-diagnostics", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ae_roster_candidates.db'}")
    return get_session_factory(engine)


def _seed_market(
    session,
    *,
    title: str,
    series_ticker: str = "KXWNBA",
    event_ticker: str = "KXWNBA-FEVER",
):
    now = utc_now()
    market = upsert_market(
        session,
        {
            "ticker": f"SPORTS-ROSTER-DIAG-{abs(hash(title))}",
            "status": "open",
            "title": title,
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "close_time": (now + timedelta(hours=6)).isoformat(),
        },
    )
    session.flush()
    return market


def _seed_partial_link(session, ticker: str, *, league: str = "WNBA") -> None:
    insert_sports_market_link(
        session,
        ticker=ticker,
        league=league,
        game_key=f"{league}:market-derived:{ticker.lower()}",
        market_type="PLAYER_PROP",
        link_confidence=Decimal("0.50"),
        link_reason="Market-derived fallback link.",
        matched_terms=["wnba", "player_prop", "market_derived"],
        raw_json={"source": "market-derived-fallback"},
    )


def _seed_verified_game(
    session,
    *,
    league: str = "WNBA",
    game_key: str,
    scheduled_delta: timedelta = timedelta(hours=6),
    home_key: str,
    home_name: str,
    home_abbreviation: str,
    away_key: str,
    away_name: str,
    away_abbreviation: str,
) -> None:
    now = utc_now()
    upsert_sports_team(
        session,
        {"team_key": home_key, "team_name": home_name, "abbreviation": home_abbreviation},
        league=league,
    )
    upsert_sports_team(
        session,
        {"team_key": away_key, "team_name": away_name, "abbreviation": away_abbreviation},
        league=league,
    )
    upsert_sports_game(
        session,
        {
            "game_key": game_key,
            "scheduled_at": (now + scheduled_delta).isoformat(),
            "home_team_key": home_key,
            "away_team_key": away_key,
            "status": "scheduled",
            "venue": "Test Arena",
        },
        league=league,
    )


def _seed_market_leg(
    session,
    ticker: str,
    *,
    entity_name: str,
    leg_index: int = 0,
    category: str = "sports",
    market_type: str = "PLAYER_PROP",
) -> None:
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=leg_index,
            parsed_at=utc_now(),
            side="yes",
            category=category,
            market_type=market_type,
            entity_name=entity_name,
            operator="gte",
            threshold_value="20",
            unit="points",
            confidence="0.90",
            raw_text=entity_name,
            reason="test player prop",
            raw_json=json.dumps({"source": "test"}),
        )
    )
    session.flush()


def _write_roster_evidence(tmp_path) -> Path:
    path = Path(tmp_path) / "phase3ah_verified_roster_evidence.json"
    path.write_text(
        json.dumps(
            [
                {
                    "league": "WNBA",
                    "player_name": "Caitlin Clark",
                    "canonical_player_id": "wnba:player:1642286",
                    "current_team_key": "WNBA:ind",
                    "current_team_name": "Indiana Fever",
                    "roster_source_url": "https://www.wnba.com/player/1642286/caitlin-clark",
                    "valid_from": "2020-01-01",
                    "valid_to": "",
                    "review_status": "VERIFIED",
                    "safe_to_apply": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_soccer_roster_evidence(tmp_path) -> Path:
    path = Path(tmp_path) / "phase3ah_verified_soccer_roster_evidence.json"
    path.write_text(
        json.dumps(
            [
                {
                    "league": "SOCCER",
                    "player_name": "Vinicius Junior",
                    "canonical_player_id": "soccer:player:vinicius-junior",
                    "current_team_key": "SOCCER:bra",
                    "current_team_name": "Brazil",
                    "roster_source_url": "https://www.fifa.com/",
                    "valid_from": "2020-01-01",
                    "valid_to": "",
                    "review_status": "VERIFIED",
                    "safe_to_apply": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path
