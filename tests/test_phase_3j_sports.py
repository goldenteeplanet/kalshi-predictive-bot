import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import (
    SignalEvent,
    SportsFeature,
    SportsGame,
    SportsInjury,
    SportsMarketLink,
    SportsSignal,
    SportsTeam,
    SportsTeamStat,
)
from kalshi_predictor.forecasting.mlb_v1 import MLBV1Forecaster
from kalshi_predictor.forecasting.sports_v1 import SportsV1Forecaster
from kalshi_predictor.scheduler import scheduler_plan
from kalshi_predictor.sports.classifier import classify_sports_market
from kalshi_predictor.sports.features import build_sports_features
from kalshi_predictor.sports.ingestion import ingest_sports_file
from kalshi_predictor.sports.injuries import injury_edge
from kalshi_predictor.sports.linker import link_sports_markets
from kalshi_predictor.sports.odds import moneyline_to_implied_probability, remove_vig
from kalshi_predictor.sports.reports import generate_sports_report
from kalshi_predictor.sports.rest import rest_edge
from kalshi_predictor.sports.signals import generate_sports_signals
from kalshi_predictor.sports.team_strength import team_strength_edge
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.utils.time import utc_now


def test_sports_json_and_csv_ingestion(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    json_path = _write_sports_json(tmp_path)
    csv_path = Path(tmp_path) / "sports.csv"
    csv_path.write_text(
        "record_type,team_key,team_name,abbreviation,game_key,home_team,away_team,"
        "scheduled_at,wins,losses,player_name,status,impact_score,home_moneyline,"
        "away_moneyline\n"
        "team,KC,Kansas City Chiefs,KC,,,,,,,,,,,\n"
        "game,,, ,NFL:chiefs-raiders,KC,LV,2026-06-18T00:00:00+00:00,,,,,,,\n"
        "team_stat,KC,,,,,,2026-06-17T00:00:00+00:00,12,5,,,,,\n",
        encoding="utf-8",
    )

    with session_factory() as session:
        json_summary = ingest_sports_file(session, league="MLB", input_file=json_path)
        csv_summary = ingest_sports_file(session, league="NFL", input_file=csv_path)
        session.commit()
        team_count = session.scalar(select(func.count(SportsTeam.id)))
        game_count = session.scalar(select(func.count(SportsGame.id)))

    assert json_summary.teams_inserted == 2
    assert json_summary.games_inserted == 1
    assert json_summary.team_stats_inserted == 2
    assert json_summary.injuries_inserted == 1
    assert json_summary.odds_inserted == 1
    assert csv_summary.games_inserted == 1
    assert team_count == 3
    assert game_count == 2


def test_classifier_detects_sports_leagues_and_market_types() -> None:
    moneyline = classify_sports_market(
        {
            "ticker": "KXMLB-DODGERS",
            "title": "Will the Dodgers beat the Yankees in tonight's MLB game?",
        }
    )
    total = classify_sports_market(
        {
            "ticker": "KXMLB-TOTAL",
            "title": "Will Dodgers and Yankees combine for over 8.5 runs?",
        }
    )

    assert moneyline["league"] == "MLB"
    assert moneyline["market_type"] == "MONEYLINE"
    assert total["league"] == "MLB"
    assert total["market_type"] == "TOTAL"


def test_linker_features_signals_and_ui_pages(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    report_path = Path(tmp_path) / "sports_report.md"
    with session_factory() as session:
        snapshot = _seed_sports_world(session, tmp_path)
        summary = link_sports_markets(
            session,
            league="MLB",
            settings=Settings(
                overnight_require_market_data=False,
                sports_min_link_confidence=Decimal("0.40"),
            ),
        )
        feature_summary = build_sports_features(
            session,
            league="MLB",
            settings=Settings(overnight_require_market_data=False),
        )
        signal_summary = generate_sports_signals(
            session,
            league="MLB",
            settings=Settings(overnight_require_market_data=False),
        )
        path = generate_sports_report(
            session,
            league="ALL",
            output_path=report_path,
            settings=Settings(overnight_require_market_data=False),
        )
        session.commit()
        links = session.scalar(select(func.count(SportsMarketLink.id)))
        features = session.scalar(select(func.count(SportsFeature.id)))
        signals = session.scalar(select(func.count(SportsSignal.id)))
        events = session.scalar(select(func.count(SignalEvent.id)))

    client = TestClient(
        create_app(
            session_factory=session_factory,
            settings=Settings(overnight_require_market_data=False),
        )
    )
    sports = client.get("/sports")
    league = client.get("/sports/leagues/MLB")
    game = client.get("/sports/games/MLB:dodgers-yankees")

    assert snapshot.ticker == "SPORTS-DODGERS"
    assert summary.links_created >= 1
    assert feature_summary.features_inserted >= 1
    assert signal_summary.signals_created >= 1
    assert links and links >= 1
    assert features and features >= 1
    assert signals and signals >= 1
    assert events and events >= 1
    assert path.exists()
    assert "Sports Intelligence Report" in path.read_text(encoding="utf-8")
    assert sports.status_code == 200
    assert "Sports Intelligence" in sports.text
    assert league.status_code == 200
    assert "MLB Sports Intelligence" in league.text
    assert game.status_code == 200
    assert "MLB:dodgers-yankees" not in game.text


def test_sports_linker_quarantines_broad_game_fanout(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    now = utc_now()
    payload = {
        "league": "MLB",
        "teams": [
            {"team_key": "LAD", "team_name": "Los Angeles Dodgers", "abbreviation": "LAD"},
            {"team_key": "NYY", "team_name": "New York Yankees", "abbreviation": "NYY"},
        ],
        "games": [
            {
                "game_key": f"MLB:dodgers-yankees-{index}",
                "scheduled_at": (now + timedelta(hours=index)).isoformat(),
                "home_team_key": "LAD",
                "away_team_key": "NYY",
                "status": "scheduled",
                "venue": "Dodger Stadium",
            }
            for index in range(1, 8)
        ],
    }
    sports_path = Path(tmp_path) / "broad_sports.json"
    sports_path.write_text(json.dumps(payload), encoding="utf-8")

    with session_factory() as session:
        ingest_sports_file(session, league="MLB", input_file=sports_path)
        insert_market_snapshot(
            session,
            {
                "ticker": "SPORTS-BROAD-DODGERS-YANKEES",
                "status": "open",
                "title": "Will the Dodgers beat the Yankees in an MLB game?",
                "series_ticker": "KXMLB",
                "event_ticker": "KXMLB-BROAD",
                "close_time": (now + timedelta(hours=6)).isoformat(),
                "yes_ask_dollars": "0.52",
                "yes_bid_dollars": "0.48",
            },
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.48", "20"]],
                    "no_dollars": [["0.48", "20"]],
                }
            },
            now,
        )

        summary = link_sports_markets(
            session,
            league="MLB",
            settings=Settings(
                overnight_require_market_data=False,
                sports_min_link_confidence=Decimal("0.40"),
                sports_max_direct_links_per_market=3,
            ),
        )
        links = list(session.scalars(select(SportsMarketLink)))

    assert summary.broad_matches_rejected == 1
    assert summary.direct_candidate_links_rejected == 7
    assert summary.market_derived_links == 1
    assert len(links) == 1
    assert "Broad sports match rejected" in links[0].link_reason
    assert json.loads(links[0].raw_json)["source"] == "broad-match-quarantine"


def test_moneyline_rest_injury_and_team_strength_math() -> None:
    assert moneyline_to_implied_probability("-150") == Decimal("0.6000")
    assert moneyline_to_implied_probability("+100") == Decimal("0.5000")
    no_vig = remove_vig(Decimal("0.60"), Decimal("0.50"))
    assert no_vig is not None
    assert no_vig[0] == Decimal("0.5455")

    now = utc_now()
    previous_home = SportsGame(
        league="MLB",
        game_key="MLB:prev-home",
        scheduled_at=now - timedelta(days=3),
        status="final",
        home_team_key="MLB:lad",
        away_team_key="MLB:other",
        raw_json="{}",
        created_at=now,
        updated_at=now,
    )
    previous_away = SportsGame(
        league="MLB",
        game_key="MLB:prev-away",
        scheduled_at=now - timedelta(days=1),
        status="final",
        home_team_key="MLB:nyy",
        away_team_key="MLB:other",
        raw_json="{}",
        created_at=now,
        updated_at=now,
    )
    rest = rest_edge(
        [previous_home, previous_away],
        home_team_key="MLB:lad",
        away_team_key="MLB:nyy",
        scheduled_at=now,
    )
    assert rest > 0

    home_injury = SportsInjury(
        league="MLB",
        team_key="MLB:lad",
        player_name="Starter",
        status="out",
        impact_score=None,
        raw_json="{}",
        created_at=now,
    )
    assert injury_edge([home_injury], []) < 0

    home_stat = SportsTeamStat(
        league="MLB",
        team_key="MLB:lad",
        as_of=now,
        wins=60,
        losses=40,
        raw_json="{}",
        created_at=now,
    )
    away_stat = SportsTeamStat(
        league="MLB",
        team_key="MLB:nyy",
        as_of=now,
        wins=40,
        losses=60,
        raw_json="{}",
        created_at=now,
    )
    assert team_strength_edge(home_stat, away_stat) > 0


def test_mlb_and_sports_v1_skip_without_data_and_forecast_with_features(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    mlb = MLBV1Forecaster(settings=Settings(overnight_require_market_data=False))
    sports = SportsV1Forecaster(settings=Settings(overnight_require_market_data=False))
    with session_factory() as session:
        unlinked = _seed_unlinked_market(session)
        assert mlb.forecast(session, unlinked) is None

        snapshot = _seed_sports_world(session, tmp_path)
        link_sports_markets(
            session,
            league="MLB",
            settings=Settings(
                overnight_require_market_data=False,
                sports_min_link_confidence=Decimal("0.40"),
            ),
        )
        build_sports_features(
            session,
            league="MLB",
            settings=Settings(overnight_require_market_data=False),
        )
        mlb_forecast = mlb.forecast(session, snapshot)
        sports_forecast = sports.forecast(session, snapshot)

    assert mlb_forecast is not None
    assert sports_forecast is not None
    assert mlb_forecast.market_mid_probability == Decimal("0.50")
    assert mlb_forecast.yes_probability > Decimal("0.50")
    assert sports_forecast.feature_json["league"] == "MLB"


def test_scheduler_profile_and_cli_smoke() -> None:
    plan = scheduler_plan("sports-watch")
    runner = CliRunner()

    assert any("build-sports-features" in step.command for step in plan)
    for command in (
        "ingest-sports",
        "link-sports-markets",
        "build-sports-features",
        "sports-report",
        "sports-opportunities",
        "sports-backtest",
        "scheduler-plan",
    ):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3j.db'}")
    return get_session_factory(engine)


def _seed_sports_world(session, tmp_path):
    ingest_sports_file(session, league="MLB", input_file=_write_sports_json(tmp_path))
    return _seed_dodgers_market(session)


def _write_sports_json(tmp_path) -> Path:
    now = utc_now()
    path = Path(tmp_path) / "sports.json"
    payload = {
        "league": "MLB",
        "teams": [
            {"team_key": "LAD", "team_name": "Los Angeles Dodgers", "abbreviation": "LAD"},
            {"team_key": "NYY", "team_name": "New York Yankees", "abbreviation": "NYY"},
        ],
        "games": [
            {
                "game_key": "MLB:dodgers-yankees",
                "scheduled_at": (now + timedelta(hours=6)).isoformat(),
                "home_team_key": "LAD",
                "away_team_key": "NYY",
                "status": "scheduled",
                "venue": "Dodger Stadium",
            }
        ],
        "team_stats": [
            {"team_key": "LAD", "as_of": now.isoformat(), "wins": 60, "losses": 40},
            {"team_key": "NYY", "as_of": now.isoformat(), "wins": 45, "losses": 55},
        ],
        "injuries": [
            {
                "team_key": "NYY",
                "player_name": "Ace Pitcher",
                "status": "out",
                "impact_score": "0.8",
                "reported_at": now.isoformat(),
            }
        ],
        "odds": [
            {
                "game_key": "MLB:dodgers-yankees",
                "sportsbook": "manual",
                "observed_at": now.isoformat(),
                "home_moneyline": "-140",
                "away_moneyline": "+120",
                "total": "8.5",
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed_dodgers_market(session):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": "SPORTS-DODGERS",
            "status": "open",
            "title": "Will the Dodgers beat the Yankees in tonight's MLB game?",
            "series_ticker": "KXMLB",
            "event_ticker": "KXMLB-DODGERS-YANKEES",
            "close_time": (now + timedelta(hours=6)).isoformat(),
            "yes_ask_dollars": "0.52",
            "yes_bid_dollars": "0.48",
            "liquidity_dollars": "12000",
            "volume_fp": "1000",
            "open_interest_fp": "500",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.48", "20"]],
                "no_dollars": [["0.48", "20"]],
            }
        },
        now,
    )


def _seed_unlinked_market(session):
    now = utc_now()
    return insert_market_snapshot(
        session,
        {
            "ticker": "SPORTS-NONE",
            "status": "open",
            "title": "Will a generic non sports event happen?",
            "series_ticker": "KXGEN",
            "event_ticker": "KXGEN",
            "close_time": (now + timedelta(hours=6)).isoformat(),
            "yes_ask_dollars": "0.52",
            "yes_bid_dollars": "0.48",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.48", "20"]],
                "no_dollars": [["0.48", "20"]],
            }
        },
        now,
    )
