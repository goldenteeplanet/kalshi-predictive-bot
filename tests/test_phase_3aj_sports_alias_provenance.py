import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import decode_json, encode_json, upsert_market
from kalshi_predictor.data.schema import MarketLeg, SportsTeam
from kalshi_predictor.phase3aj import (
    build_sports_alias_provenance_repair,
    write_phase3aj_report,
)
from kalshi_predictor.sports.repository import insert_sports_market_link, upsert_sports_team
from kalshi_predictor.utils.time import utc_now


def test_phase3aj_classifies_multileg_market_without_forcing_verified_link(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-P3AJ",
                "status": "open",
                "title": "yes Chicago C,yes Texas,yes Chicago WS,yes Baltimore",
                "series_ticker": "KXMVESPORTSMULTIGAMEEXTENDED",
                "close_time": (utc_now() + timedelta(hours=8)).isoformat(),
            },
        )
        for index, entity in enumerate(("Chicago C", "Texas", "Chicago WS", "Baltimore")):
            _seed_leg(session, market.ticker, entity, index=index)
        _seed_partial_link(session, market.ticker, league="MLB")

        payload = build_sports_alias_provenance_repair(session)

    assert payload["summary"]["partial_markets_reviewed"] == 1
    assert payload["summary"]["multi_leg_markets"] == 1
    assert payload["rows"][0]["reason"] == "MULTI_LEG_MARKET_REQUIRES_COMPETITION_PROVENANCE"
    assert "do not force" in payload["rows"][0]["next_action"].lower()


def test_phase3aj_suggests_and_applies_observed_soccer_alias(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        city, _ = upsert_sports_team(
            session,
            {"team_key": "MCI", "team_name": "Manchester City", "abbreviation": "MCI"},
            league="SOCCER",
        )
        upsert_sports_team(
            session,
            {"team_key": "ARS", "team_name": "Arsenal", "abbreviation": "ARS"},
            league="SOCCER",
        )
        _remove_raw_alias(city, "man city")
        market = upsert_market(
            session,
            {
                "ticker": "SOCCER-MAN-CITY-P3AJ",
                "status": "open",
                "title": "Will Man City beat Arsenal in the Premier League?",
                "series_ticker": "KXSOCCER",
                "event_ticker": "KXSOCCER-MAN-CITY-ARSENAL",
                "close_time": (utc_now() + timedelta(hours=4)).isoformat(),
            },
        )
        _seed_leg(session, market.ticker, "Man City")
        _seed_leg(session, market.ticker, "Arsenal", index=1)
        _seed_partial_link(session, market.ticker, league="SOCCER")

        payload = build_sports_alias_provenance_repair(session, apply_aliases=True)
        session.commit()
        refreshed = session.scalar(
            select(SportsTeam).where(SportsTeam.team_key == "SOCCER:mci")
        )

    assert payload["summary"]["alias_suggestions"] >= 1
    assert any(
        row["alias"] == "man city" and row["applied"]
        for row in payload["alias_suggestions"]
    )
    assert any(row["competition_code"] == "eng.1" for row in payload["competition_suggestions"])
    assert payload["rows"][0]["reason"] == "SOCCER_COMPETITION_PROVENANCE_MISSING"
    assert refreshed is not None
    assert "man city" in decode_json(refreshed.raw_json)["aliases"]


def test_phase3aj_report_writes_alias_and_competition_artifacts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "phase3aj"
    with session_factory() as session:
        market = upsert_market(
            session,
            {
                "ticker": "SOCCER-WORLDCUP-P3AJ",
                "status": "open",
                "title": "Will Brazil win this World Cup qualifying soccer match?",
                "series_ticker": "KXSOCCER",
            },
        )
        _seed_leg(session, market.ticker, "Brazil")
        _seed_partial_link(session, market.ticker, league="SOCCER")
        artifacts = write_phase3aj_report(session, output_dir=output_dir)

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    template = json.loads(artifacts.competition_template_path.read_text(encoding="utf-8"))
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert artifacts.alias_suggestions_path.exists()
    assert payload["summary"]["soccer_markets"] == 1
    assert "fifa.worldq" in template["recommended_competitions"]
    assert "Phase 3AJ" in markdown


def test_phase3aj_cli_help() -> None:
    result = CliRunner().invoke(app, ["phase3aj-sports-alias-provenance", "--help"])

    assert result.exit_code == 0
    assert "phase3aj-sports-alias-provenance" in result.output


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3aj.db'}")
    return get_session_factory(engine)


def _seed_leg(session, ticker: str, entity: str, *, index: int = 0) -> None:
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
            confidence="0.80",
            raw_text=f"yes {entity}",
            reason="test sports leg",
            raw_json="{}",
        )
    )


def _seed_partial_link(session, ticker: str, *, league: str) -> None:
    insert_sports_market_link(
        session,
        ticker=ticker,
        league=league,
        game_key=f"{league}:market-derived:{ticker.lower()}",
        market_type="MONEYLINE",
        link_confidence=Decimal("0.50"),
        link_reason="Market-derived fallback link.",
        matched_terms=[league.lower(), "market_derived"],
        raw_json={"source": "market-derived-fallback"},
    )


def _remove_raw_alias(team: SportsTeam, alias: str) -> None:
    raw = decode_json(team.raw_json)
    raw["aliases"] = [item for item in raw.get("aliases", []) if item != alias]
    team.raw_json = encode_json(raw)
