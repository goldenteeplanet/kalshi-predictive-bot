import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market
from kalshi_predictor.data.schema import MarketLeg, PaperOrder, SportsFeature, SportsMarketLink
from kalshi_predictor.phase3ae import (
    _is_round_placeholder_team_key,
    run_verified_sports_schedule_connector,
)
from kalshi_predictor.sports.repository import (
    insert_sports_market_link,
    upsert_sports_game,
    upsert_sports_team,
)
from kalshi_predictor.utils.time import utc_now


def test_phase3ae_upgrades_partial_link_with_verified_schedule(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_dodgers_market(session)
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(session, game_key="MLB:dodgers-yankees")

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.40")),
        )
        session.commit()
        links = list(session.scalars(select(SportsMarketLink).order_by(SportsMarketLink.id)))
        feature_count = session.scalar(select(func.count(SportsFeature.id)))
        paper_order_count = session.scalar(select(func.count(PaperOrder.id)))

    assert payload["summary"]["verified_links_created"] == 1
    assert payload["summary"]["features_created"] == 1
    assert payload["after"]["provenance_counts"]["verified_schedule"] == 1
    assert payload["after"]["partial_without_upgrade"] == 0
    assert len(links) == 2
    assert links[-1].game_key == "MLB:dodgers-yankees"
    assert "verified_schedule" in links[-1].raw_json
    assert feature_count == 1
    assert paper_order_count == 0


def test_phase3ae_skips_ambiguous_verified_schedule_matches(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_dodgers_market(session)
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(session, game_key="MLB:dodgers-yankees-a")
        _seed_verified_game(session, game_key="MLB:dodgers-yankees-b")

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.40")),
        )
        session.commit()
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 0
    assert payload["summary"]["ambiguous_matches"] == 1
    assert payload["summary"]["manual_disambiguation_candidates"] == 1
    assert payload["rows"][0]["status"] == "AMBIGUOUS_MATCH"
    assert len(payload["rows"][0]["candidate_games"]) == 2
    assert payload["manual_disambiguation_candidates"][0]["ticker"] == market.ticker
    assert payload["manual_disambiguation_candidates"][0]["review_status"] == "UNVERIFIED"
    assert payload["manual_disambiguation_candidates"][0]["safe_to_upgrade"] is False
    assert len(payload["manual_disambiguation_candidates"][0]["candidate_games"]) == 2
    assert link_count == 1


def test_phase3ae_candidate_game_key_filter_targets_safe_placeholder_game(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_dodgers_market(session)
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(session, game_key="MLB:dodgers-yankees-a")
        _seed_verified_game(session, game_key="MLB:dodgers-yankees-b")

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.40")),
            candidate_game_keys={"MLB:dodgers-yankees-b"},
        )
        session.commit()
        links = list(session.scalars(select(SportsMarketLink).order_by(SportsMarketLink.id)))

    assert payload["candidate_game_keys"] == ["MLB:dodgers-yankees-b"]
    assert payload["summary"]["candidate_game_key_filter_count"] == 1
    assert payload["summary"]["verified_games_seen"] == 1
    assert payload["summary"]["verified_links_created"] == 1
    assert payload["summary"]["ambiguous_matches"] == 0
    assert links[-1].game_key == "MLB:dodgers-yankees-b"


def test_phase3ae_reports_missing_verified_schedule_data(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_dodgers_market(session)
        _seed_partial_link(session, market.ticker)

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.40")),
        )

    assert payload["summary"]["verified_games_seen"] == 0
    assert payload["summary"]["no_verified_game"] == 1
    assert payload["summary"]["verified_links_created"] == 0
    assert "phase3af-sports-schedule-bootstrap" in payload["recommended_next_action"]


def test_phase3ae_blocks_stale_schedule_mismatch(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_dodgers_market(session)
        market.close_time = utc_now()
        market.status = "finalized"
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(
            session,
            game_key="MLB:dodgers-yankees-stale",
            scheduled_delta=timedelta(days=2),
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.40")),
            max_schedule_delta_hours=18,
        )
        session.commit()
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 0
    assert payload["summary"]["no_verified_match"] == 1
    assert link_count == 1


def test_phase3ae_skips_classification_when_schedule_window_has_no_games(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_dodgers_market(session)
        market.close_time = utc_now()
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(
            session,
            game_key="MLB:dodgers-yankees-outside-window",
            scheduled_delta=timedelta(days=3),
        )

        def fail_classification(*args, **kwargs):
            raise AssertionError("classification should be skipped outside schedule window")

        monkeypatch.setattr(
            "kalshi_predictor.phase3ae.classify_sports_market",
            fail_classification,
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.40")),
            max_schedule_delta_hours=18,
        )

    assert payload["summary"]["verified_links_created"] == 0
    assert payload["summary"]["no_verified_match"] == 1


def test_phase3ae_blocks_conflicting_team_mentions(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_dodgers_market(session)
        market.title = "Will the Dodgers and Red Sox both win?"
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(session, game_key="MLB:dodgers-yankees")
        _seed_verified_game(
            session,
            game_key="MLB:red-sox-orioles",
            home_key="BOS",
            home_name="Boston Red Sox",
            home_abbreviation="BOS",
            away_key="BAL",
            away_name="Baltimore Orioles",
            away_abbreviation="BAL",
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.40")),
        )
        session.commit()
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 0
    assert payload["summary"]["no_verified_match"] == 1
    assert link_count == 1


def test_phase3ae_does_not_treat_san_francisco_as_round_placeholder() -> None:
    assert _is_round_placeholder_team_key("MLB:sf") is False
    assert _is_round_placeholder_team_key("SOCCER:sf-w1") is True


def test_phase3ae_applies_reviewed_team_alias_evidence(tmp_path) -> None:
    alias_path = _write_team_alias_evidence(tmp_path, safe=True)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_blue_crew_market(session)
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(
            session,
            game_key="MLB:dodgers-giants",
            away_key="SF",
            away_name="San Francisco Giants",
            away_abbreviation="SF",
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.50")),
            build_features=False,
            team_alias_review_path=alias_path,
        )
        session.commit()
        links = list(session.scalars(select(SportsMarketLink).order_by(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 1
    assert payload["summary"]["team_alias_evidence_rows_seen"] == 1
    assert payload["summary"]["team_alias_evidence_rows_applied"] == 1
    assert payload["summary"]["team_alias_verified_links_created"] == 1
    assert links[-1].game_key == "MLB:dodgers-giants"
    raw = json.loads(links[-1].raw_json)
    assert raw["match_source"] == "phase3ah_team_alias_review"
    assert raw["team_alias_evidence"][0]["alias"] == "Blue Crew"


def test_phase3ae_ignores_unreviewed_team_alias_evidence(tmp_path) -> None:
    alias_path = _write_team_alias_evidence(tmp_path, safe=False)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_blue_crew_market(session)
        _seed_partial_link(session, market.ticker)
        _seed_verified_game(
            session,
            game_key="MLB:dodgers-giants",
            away_key="SF",
            away_name="San Francisco Giants",
            away_abbreviation="SF",
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.50")),
            build_features=False,
            team_alias_review_path=alias_path,
        )
        session.commit()
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 0
    assert payload["summary"]["team_alias_evidence_rows_seen"] == 0
    assert payload["summary"]["team_alias_evidence_rows_applied"] == 0
    assert payload["summary"]["no_verified_match"] == 1
    assert link_count == 1


def test_phase3ae_applies_reviewed_manual_disambiguation(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_manual_disambiguation_market(session)
        manual_path = _write_manual_disambiguation_evidence(
            tmp_path,
            ticker=market.ticker,
            chosen_game_key="MLB:dodgers-giants",
        )
        _seed_partial_link(session, market.ticker, market_type="TOTAL")
        _seed_verified_game(
            session,
            game_key="MLB:dodgers-giants",
            away_key="SF",
            away_name="San Francisco Giants",
            away_abbreviation="SF",
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.50")),
            build_features=False,
            manual_disambiguation_path=manual_path,
        )
        session.commit()
        links = list(session.scalars(select(SportsMarketLink).order_by(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 1
    assert payload["summary"]["manual_disambiguation_rows_seen"] == 1
    assert payload["summary"]["manual_disambiguation_rows_applied"] == 1
    assert payload["summary"]["manual_disambiguation_links_created"] == 1
    assert links[-1].game_key == "MLB:dodgers-giants"
    raw = json.loads(links[-1].raw_json)
    assert raw["match_source"] == "phase3ah_manual_disambiguation"
    assert raw["manual_disambiguation"]["chosen_game_key"] == "MLB:dodgers-giants"


def test_phase3ae_upgrades_player_prop_with_verified_roster_evidence(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_caitlin_market(session)
        _seed_partial_link(session, market.ticker, league="WNBA", market_type="PLAYER_PROP")
        _seed_verified_game(
            session,
            league="WNBA",
            game_key="WNBA:fever-sparks",
            home_key="IND",
            home_name="Indiana Fever",
            home_abbreviation="IND",
            away_key="LAS",
            away_name="Los Angeles Sparks",
            away_abbreviation="LAS",
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.80")),
            build_features=False,
            roster_evidence_path=evidence_path,
        )
        session.commit()
        links = list(session.scalars(select(SportsMarketLink).order_by(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 1
    assert payload["summary"]["roster_verified_links_created"] == 1
    assert payload["summary"]["roster_evidence_rows_seen"] == 1
    assert links[-1].game_key == "WNBA:fever-sparks"
    raw = json.loads(links[-1].raw_json)
    assert raw["match_source"] == "phase3ah_roster_evidence"
    assert raw["roster_evidence"][0]["player_name"] == "Caitlin Clark"
    assert raw["roster_evidence"][0]["current_team_key"] == "WNBA:ind"


def test_phase3ae_blocks_roster_player_when_team_not_in_game(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_caitlin_market(session)
        _seed_partial_link(session, market.ticker, league="WNBA", market_type="PLAYER_PROP")
        _seed_verified_game(
            session,
            league="WNBA",
            game_key="WNBA:sparks-liberty",
            home_key="LAS",
            home_name="Los Angeles Sparks",
            home_abbreviation="LAS",
            away_key="NYL",
            away_name="New York Liberty",
            away_abbreviation="NYL",
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.80")),
            build_features=False,
            roster_evidence_path=evidence_path,
        )
        session.commit()
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 0
    assert payload["summary"]["no_verified_match"] == 1
    assert link_count == 1


def test_phase3ae_blocks_ambiguous_roster_schedule_matches(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_caitlin_market(session)
        _seed_partial_link(session, market.ticker, league="WNBA", market_type="PLAYER_PROP")
        _seed_verified_game(
            session,
            league="WNBA",
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
            league="WNBA",
            game_key="WNBA:fever-liberty",
            home_key="IND",
            home_name="Indiana Fever",
            home_abbreviation="IND",
            away_key="NYL",
            away_name="New York Liberty",
            away_abbreviation="NYL",
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.80")),
            build_features=False,
            roster_evidence_path=evidence_path,
        )
        session.commit()
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 0
    assert payload["summary"]["ambiguous_matches"] == 1
    assert link_count == 1


def test_phase3ae_blocks_roster_upgrade_when_extra_player_leg_is_unverified(tmp_path) -> None:
    evidence_path = _write_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_caitlin_market(session)
        market.title = "Will Caitlin Clark and Courtney Williams each score 20+ points tonight?"
        _seed_partial_link(session, market.ticker, league="WNBA", market_type="PLAYER_PROP")
        _seed_market_leg(session, market.ticker, entity_name="Caitlin Clark")
        _seed_market_leg(session, market.ticker, entity_name="Courtney Williams", leg_index=1)
        _seed_verified_game(
            session,
            league="WNBA",
            game_key="WNBA:fever-sparks",
            home_key="IND",
            home_name="Indiana Fever",
            home_abbreviation="IND",
            away_key="LAS",
            away_name="Los Angeles Sparks",
            away_abbreviation="LAS",
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.80")),
            build_features=False,
            roster_evidence_path=evidence_path,
        )
        session.commit()
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 0
    assert payload["summary"]["roster_verified_links_created"] == 0
    assert payload["summary"]["no_verified_match"] == 1
    assert link_count == 1


def test_phase3ae_blocks_roster_upgrade_for_round_placeholder_game(tmp_path) -> None:
    evidence_path = _write_placeholder_roster_evidence(tmp_path)
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = _seed_caitlin_market(session)
        _seed_partial_link(session, market.ticker, league="WNBA", market_type="PLAYER_PROP")
        _seed_verified_game(
            session,
            league="WNBA",
            game_key="WNBA:rd16-w1-sparks",
            home_key="RD16-W1",
            home_name="Round of 16 Winner 1",
            home_abbreviation="RD16W1",
            away_key="LAS",
            away_name="Los Angeles Sparks",
            away_abbreviation="LAS",
        )

        payload = run_verified_sports_schedule_connector(
            session,
            settings=Settings(sports_min_link_confidence=Decimal("0.80")),
            build_features=False,
            roster_evidence_path=evidence_path,
        )
        session.commit()
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["verified_links_created"] == 0
    assert payload["summary"]["roster_verified_links_created"] == 0
    assert payload["summary"]["no_verified_match"] == 1
    assert link_count == 1


def test_phase3ae_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3ae-verified-sports-connector", "--help"])

    assert result.exit_code == 0
    assert "Usage" in result.output
    assert "--candidate-game-key" in result.output
    assert "reviewed team alias" in result.output
    assert "manual" in result.output
    assert "disambiguation" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3ae.db'}")
    return get_session_factory(engine)


def _seed_dodgers_market(session):
    now = utc_now()
    market = upsert_market(
        session,
        {
            "ticker": "SPORTS-DODGERS-P3AE",
            "status": "open",
            "title": "Will the Dodgers beat the Yankees in tonight's MLB game?",
            "series_ticker": "KXMLB",
            "event_ticker": "KXMLB-DODGERS-YANKEES",
            "close_time": (now + timedelta(hours=6)).isoformat(),
        },
    )
    session.flush()
    return market


def _seed_caitlin_market(session):
    now = utc_now()
    market = upsert_market(
        session,
        {
            "ticker": "SPORTS-CAITLIN-P3AE",
            "status": "open",
            "title": "Will Caitlin Clark score 20+ points tonight?",
            "series_ticker": "KXWNBA",
            "event_ticker": "KXWNBA-FEVER",
            "close_time": (now + timedelta(hours=6)).isoformat(),
        },
    )
    session.flush()
    return market


def _seed_blue_crew_market(session):
    now = utc_now()
    market = upsert_market(
        session,
        {
            "ticker": "SPORTS-BLUECREW-P3AE",
            "status": "open",
            "title": "Will the Blue Crew win tonight?",
            "series_ticker": "KXBASE",
            "event_ticker": "KXBASE-GAME",
            "close_time": (now + timedelta(hours=6)).isoformat(),
        },
    )
    session.flush()
    return market


def _seed_manual_disambiguation_market(session):
    now = utc_now()
    market = upsert_market(
        session,
        {
            "ticker": "SPORTS-MANUAL-P3AE",
            "status": "open",
            "title": "Will the listed baseball slate finish over the target?",
            "series_ticker": "KXBASE",
            "event_ticker": "KXBASE-SLATE",
            "close_time": (now + timedelta(hours=6)).isoformat(),
        },
    )
    session.flush()
    return market


def _seed_partial_link(
    session,
    ticker: str,
    *,
    league: str = "MLB",
    market_type: str = "MONEYLINE",
) -> None:
    insert_sports_market_link(
        session,
        ticker=ticker,
        league=league,
        game_key=f"{league}:market-derived:{ticker.lower()}",
        market_type=market_type,
        link_confidence=Decimal("0.50"),
        link_reason="Market-derived fallback link.",
        matched_terms=[league.lower(), market_type.lower(), "market_derived"],
        raw_json={"source": "market-derived-fallback"},
    )


def _seed_verified_game(
    session,
    *,
    league: str = "MLB",
    game_key: str,
    scheduled_delta: timedelta = timedelta(hours=6),
    home_key: str = "LAD",
    home_name: str = "Los Angeles Dodgers",
    home_abbreviation: str = "LAD",
    away_key: str = "NYY",
    away_name: str = "New York Yankees",
    away_abbreviation: str = "NYY",
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
            "venue": "Dodger Stadium",
        },
        league=league,
    )


def _seed_market_leg(
    session,
    ticker: str,
    *,
    entity_name: str,
    leg_index: int = 0,
) -> None:
    session.add(
        MarketLeg(
            ticker=ticker,
            leg_index=leg_index,
            parsed_at=utc_now(),
            side="yes",
            category="sports",
            market_type="PLAYER_PROP",
            entity_name=entity_name,
            operator="gte",
            threshold_value="20",
            unit="points",
            confidence="0.90",
            raw_text=f"yes {entity_name}: 20+",
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
                    "valid_from": "2026-06-27",
                    "valid_to": "",
                    "review_status": "VERIFIED",
                    "safe_to_apply": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_team_alias_evidence(tmp_path, *, safe: bool) -> Path:
    path = Path(tmp_path) / "phase3ah_team_alias_review_template.json"
    path.write_text(
        json.dumps(
            [
                {
                    "league": "MLB",
                    "alias_to_add": "Blue Crew",
                    "canonical_team_key": "MLB:lad",
                    "canonical_team_name": "Los Angeles Dodgers",
                    "evidence_source_url": "https://example.test/dodgers-blue-crew",
                    "review_status": "VERIFIED" if safe else "UNVERIFIED",
                    "safe_to_apply": safe,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_manual_disambiguation_evidence(
    tmp_path,
    *,
    ticker: str,
    chosen_game_key: str,
) -> Path:
    path = Path(tmp_path) / "phase3ah_manual_disambiguation_template.json"
    path.write_text(
        json.dumps(
            [
                {
                    "ticker": ticker,
                    "league": "MLB",
                    "chosen_game_key": chosen_game_key,
                    "chosen_market_type": "TOTAL",
                    "verification_source_url": "https://example.test/manual-game",
                    "review_status": "VERIFIED",
                    "safe_to_upgrade": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_placeholder_roster_evidence(tmp_path) -> Path:
    path = Path(tmp_path) / "phase3ah_placeholder_roster_evidence.json"
    path.write_text(
        json.dumps(
            [
                {
                    "league": "WNBA",
                    "player_name": "Caitlin Clark",
                    "canonical_player_id": "wnba:player:1642286",
                    "current_team_key": "WNBA:rd16-w1",
                    "current_team_name": "Round of 16 Winner 1",
                    "roster_source_url": "https://www.wnba.com/player/1642286/caitlin-clark",
                    "valid_from": "2026-06-27",
                    "valid_to": "",
                    "review_status": "VERIFIED",
                    "safe_to_apply": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path
