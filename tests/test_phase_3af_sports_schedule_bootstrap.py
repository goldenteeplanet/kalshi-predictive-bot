from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor import phase3af
from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import decode_json, encode_json, upsert_market
from kalshi_predictor.data.schema import MarketSnapshot, SportsGame, SportsMarketLink, SportsTeam
from kalshi_predictor.phase3af import build_phase3af_coverage_diagnostics, write_phase3af_report
from kalshi_predictor.sports.classifier import classify_sports_market
from kalshi_predictor.sports.ingestion import ingest_sports_payload
from kalshi_predictor.sports.linker import score_sports_market_link
from kalshi_predictor.sports.repository import (
    insert_sports_feature,
    insert_sports_market_link,
    sports_team_aliases,
    upsert_sports_game,
    upsert_sports_team,
)
from kalshi_predictor.utils.time import utc_now


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def test_phase3af_fetches_writes_and_ingests_verified_schedule(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(phase3af.httpx, "get", lambda *_args, **_kwargs: _Response(_scoreboard()))
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        artifacts = write_phase3af_report(
            session,
            output_dir=Path(tmp_path) / "reports",
            schedule_output_dir=Path(tmp_path) / "schedules",
            leagues="MLB",
            start_date="2026-06-25",
            days_ahead=1,
            ingest=True,
            write_legacy_sample=False,
        )
        session.commit()
        team_count = session.scalar(select(func.count(SportsTeam.id)))
        game = session.scalar(select(SportsGame).limit(1))

    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert len(artifacts.schedule_paths) == 1
    assert team_count == 2
    assert game is not None
    assert game.game_key == "MLB:espn:mlb:401"
    assert decode_json(game.raw_json)["source"] == "espn_scoreboard"


def test_phase3af_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3af-sports-schedule-bootstrap", "--help"])

    assert result.exit_code == 0
    assert "phase3af-sports-schedule-bootstrap" in result.output


def test_phase3af_local_fixture_normalizes_timezone_status_and_aliases(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    payload = {
        "league": "SOCCER",
        "teams": [
            {
                "team_key": "NOR",
                "team_name": "Norwich City",
                "abbreviation": "NOR",
                "aliases": ["Canaries", "Norwich"],
            },
            {
                "team_key": "LIV",
                "team_name": "Liverpool",
                "abbreviation": "LIV",
            },
        ],
        "games": [
            {
                "game_key": "fixture-1",
                "scheduled_at": "2026-06-25T19:30:00",
                "source_timezone": "America/Chicago",
                "status": "STATUS_POSTPONED",
                "home_team_key": "NOR",
                "away_team_key": "LIV",
                "venue": "Carrow Road",
            }
        ],
    }
    with session_factory() as session:
        summary = ingest_sports_payload(session, payload, league="SOCCER", source="fixture")
        session.commit()
        team = session.scalar(select(SportsTeam).where(SportsTeam.team_key == "SOCCER:nor"))
        game = session.scalar(select(SportsGame).where(SportsGame.game_key == "SOCCER:fixture-1"))

    assert summary.errors == []
    assert team is not None
    assert "canaries" in sports_team_aliases(team)
    assert game is not None
    assert game.status == "postponed"
    assert game.scheduled_at is not None
    assert game.scheduled_at.isoformat().startswith("2026-06-26T00:30:00")


def test_phase3af_soccer_country_goal_market_is_not_unknown() -> None:
    classification = classify_sports_market(
        {
            "ticker": "KX-SOCCER-COUNTRIES",
            "title": "yes Morocco, yes Brazil, no Over 2.5 goals scored",
        }
    )

    assert classification["league"] == "SOCCER"
    assert "morocco" in classification["matched_terms"]


def test_sports_linker_uses_canonical_team_aliases(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "SPORTS-CANARIES-P3AF",
                "title": "Will the Canaries beat Liverpool?",
                "series_ticker": "KXSOCCER",
                "event_ticker": "KXSOCCER-NOR-LIV",
                "status": "open",
                "close_time": (utc_now() + timedelta(hours=2)).isoformat(),
            },
        )
        home, _ = upsert_sports_team(
            session,
            {
                "team_key": "NOR",
                "team_name": "Norwich City",
                "abbreviation": "NOR",
                "aliases": ["Canaries"],
            },
            league="SOCCER",
        )
        away, _ = upsert_sports_team(
            session,
            {"team_key": "LIV", "team_name": "Liverpool", "abbreviation": "LIV"},
            league="SOCCER",
        )
        game, _ = upsert_sports_game(
            session,
            {
                "game_key": "SOCCER:norwich-liverpool",
                "scheduled_at": (utc_now() + timedelta(hours=2)).isoformat(),
                "status": "scheduled",
                "home_team_key": "NOR",
                "away_team_key": "LIV",
            },
            league="SOCCER",
        )

        confidence, _reason, matched_terms, market_type = score_sports_market_link(
            market,
            game,
            home_team=home,
            away_team=away,
            classification=classify_sports_market(
                market,
                teams=[
                    {
                        "league": "SOCCER",
                        "team_key": home.team_key,
                        "team_name": home.team_name,
                        "aliases": sports_team_aliases(home),
                    },
                    {
                        "league": "SOCCER",
                        "team_key": away.team_key,
                        "team_name": away.team_name,
                        "aliases": sports_team_aliases(away),
                    },
                ],
            ),
        )

    assert confidence >= Decimal("0.80")
    assert "canaries" in matched_terms
    assert market_type == "MONEYLINE"


def test_phase3af_coverage_reports_paper_only_golden_trace(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        phase3af.httpx,
        "get",
        lambda *_args, **_kwargs: _Response(_empty_scoreboard()),
    )
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_ready_sports_trace(session)

        artifacts = write_phase3af_report(
            session,
            output_dir=Path(tmp_path) / "reports",
            schedule_output_dir=Path(tmp_path) / "schedules",
            leagues="MLB",
            start_date="2026-06-25",
            days_ahead=1,
            ingest=False,
            write_legacy_sample=False,
        )
        payload = decode_json(artifacts.json_path.read_text(encoding="utf-8"))
        markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    coverage = payload["coverage_diagnostics"]
    assert coverage["summary"]["sports_v1_forecast_eligible"] == 1
    assert coverage["summary"]["linked_events_with_usable_features"] == 1
    assert coverage["golden_trace"]["status"] == "READY"
    assert coverage["golden_trace"]["kalshi_market"] == "SPORTS-DODGERS-P3AF"
    assert "## Coverage Diagnostics" in markdown
    assert "SPORTS_V1_FORECAST_ELIGIBLE" in markdown


def test_phase3af_coverage_marks_ambiguous_matches_without_linking(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        _seed_market(session, ticker="SPORTS-DODGERS-AMBIG")
        _seed_verified_game(session, game_key="MLB:espn:mlb:ambig-a")
        _seed_verified_game(session, game_key="MLB:espn:mlb:ambig-b")

        payload = build_phase3af_coverage_diagnostics(session)
        link_count = session.scalar(select(func.count(SportsMarketLink.id)))

    assert payload["summary"]["ambiguous_matches"] == 1
    assert payload["market_rows"][0]["status"] == "AMBIGUOUS_VERIFIED_MATCH"
    assert payload["market_rows"][0]["reason_code"] == "multiple_verified_games_plausible"
    assert link_count == 0


def test_ingest_sports_missing_file_prints_bootstrap_next_action(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "ingest-sports",
            "--league",
            "MLB",
            "--input-file",
            str(Path(tmp_path) / "missing_sports.json"),
        ],
        env={"KALSHI_DB_URL": f"sqlite:///{Path(tmp_path) / 'missing.db'}"},
    )

    assert result.exit_code == 2
    assert "Sports input file not found" in result.output
    assert "phase3af-sports-schedule-bootstrap" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3af.db'}")
    return get_session_factory(engine)


def _scoreboard():
    return {
        "season": {"year": 2026},
        "events": [
            {
                "id": "401",
                "date": "2026-06-25T23:10:00Z",
                "name": "Los Angeles Dodgers at New York Yankees",
                "shortName": "LAD @ NYY",
                "competitions": [
                    {
                        "date": "2026-06-25T23:10:00Z",
                        "venue": {"fullName": "Yankee Stadium"},
                        "status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED"}},
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "",
                                "team": {
                                    "id": "10",
                                    "displayName": "New York Yankees",
                                    "abbreviation": "NYY",
                                    "location": "New York",
                                },
                                "records": [{"type": "total", "summary": "45-32"}],
                            },
                            {
                                "homeAway": "away",
                                "score": "",
                                "team": {
                                    "id": "19",
                                    "displayName": "Los Angeles Dodgers",
                                    "abbreviation": "LAD",
                                    "location": "Los Angeles",
                                },
                                "records": [{"type": "total", "summary": "50-28"}],
                            },
                        ],
                    }
                ],
            }
        ],
    }


def _empty_scoreboard():
    return {"season": {"year": 2026}, "events": []}


def _seed_ready_sports_trace(session) -> None:
    market = _seed_market(session, ticker="SPORTS-DODGERS-P3AF")
    game = _seed_verified_game(session, game_key="MLB:espn:mlb:402")
    link, _ = insert_sports_market_link(
        session,
        ticker=market.ticker,
        league="MLB",
        game_key=game.game_key,
        market_type="MONEYLINE",
        link_confidence=Decimal("0.95"),
        link_reason="Phase 3AF verified schedule/team match.",
        matched_terms=["mlb", "dodgers", "yankees", "verified_schedule"],
        raw_json={"source": "verified_schedule", "phase": "3AF-test"},
    )
    insert_sports_feature(
        session,
        league="MLB",
        game_key=game.game_key,
        ticker=market.ticker,
        home_team_key=game.home_team_key,
        away_team_key=game.away_team_key,
        team_strength_edge=Decimal("0.01"),
        injury_edge=Decimal("0"),
        rest_edge=Decimal("0"),
        travel_edge=Decimal("0"),
        odds_edge=Decimal("0"),
        weather_edge=Decimal("0"),
        total_edge=Decimal("0.01"),
        home_win_probability=Decimal("0.51"),
        away_win_probability=Decimal("0.49"),
        projected_total=None,
        confidence_score=Decimal("60"),
        raw_json={"source": "test", "link_id": link.id},
    )


def _seed_market(session, *, ticker: str):
    now = utc_now()
    market = upsert_market(
        session,
        {
            "ticker": ticker,
            "status": "open",
            "title": "Will the Dodgers beat the Yankees in tonight's MLB game?",
            "series_ticker": "KXMLB",
            "event_ticker": "KXMLB-DODGERS-YANKEES",
            "close_time": (now + timedelta(hours=6)).isoformat(),
        },
    )
    session.add(
        MarketSnapshot(
            ticker=ticker,
            captured_at=now,
            status="open",
            yes_bid_dollars="0.40",
            yes_ask_dollars="0.42",
            no_bid_dollars="0.58",
            no_ask_dollars="0.60",
            best_yes_bid="0.40",
            best_yes_ask="0.42",
            best_no_bid="0.58",
            best_no_ask="0.60",
            spread="0.02",
            last_price_dollars="0.41",
            volume_fp="100",
            volume_24h_fp="20",
            open_interest_fp="10",
            raw_market_json=encode_json(
                {
                    "ticker": ticker,
                    "title": "Will the Dodgers beat the Yankees in tonight's MLB game?",
                }
            ),
            raw_orderbook_json=None,
        )
    )
    session.flush()
    return market


def _seed_verified_game(session, *, game_key: str) -> SportsGame:
    now = utc_now()
    upsert_sports_team(
        session,
        {
            "team_key": "LAD",
            "team_name": "Los Angeles Dodgers",
            "abbreviation": "LAD",
            "city": "Los Angeles",
        },
        league="MLB",
    )
    upsert_sports_team(
        session,
        {
            "team_key": "NYY",
            "team_name": "New York Yankees",
            "abbreviation": "NYY",
            "city": "New York",
        },
        league="MLB",
    )
    game, _ = upsert_sports_game(
        session,
        {
            "game_key": game_key,
            "scheduled_at": (now + timedelta(hours=6)).isoformat(),
            "home_team_key": "LAD",
            "away_team_key": "NYY",
            "status": "scheduled",
            "venue": "Dodger Stadium",
            "source": "espn_scoreboard",
            "source_url": "https://example.test/schedule",
        },
        league="MLB",
    )
    return game
