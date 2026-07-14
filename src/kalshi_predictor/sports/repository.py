from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    SportsFeature,
    SportsGame,
    SportsInjury,
    SportsMarketLink,
    SportsOdds,
    SportsSignal,
    SportsTeam,
    SportsTeamStat,
)
from kalshi_predictor.sports.aliases import supplemental_team_aliases
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

SUPPORTED_LEAGUES = {"MLB", "NBA", "NFL", "NHL", "WNBA", "SOCCER", "SPORTS", "ALL"}
CANONICAL_GAME_STATUSES = {
    "scheduled",
    "in_progress",
    "delayed",
    "postponed",
    "cancelled",
    "rescheduled",
    "final",
    "unknown",
}

STATUS_ALIASES = {
    "pre": "scheduled",
    "preview": "scheduled",
    "not_started": "scheduled",
    "not started": "scheduled",
    "scheduled": "scheduled",
    "status_scheduled": "scheduled",
    "live": "in_progress",
    "active": "in_progress",
    "in": "in_progress",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "status_in_progress": "in_progress",
    "delay": "delayed",
    "delayed": "delayed",
    "status_delayed": "delayed",
    "postponed": "postponed",
    "status_postponed": "postponed",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "status_canceled": "cancelled",
    "status_cancelled": "cancelled",
    "rescheduled": "rescheduled",
    "status_rescheduled": "rescheduled",
    "completed": "final",
    "complete": "final",
    "closed": "final",
    "final": "final",
    "post": "final",
    "status_final": "final",
}


def normalize_league(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text not in SUPPORTED_LEAGUES:
        allowed = ", ".join(sorted(SUPPORTED_LEAGUES))
        raise ValueError(f"Sports league must be one of: {allowed}.")
    return text


def normalize_sports_status(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        return "unknown"
    normalized = STATUS_ALIASES.get(text, text)
    return normalized if normalized in CANONICAL_GAME_STATUSES else "unknown"


def team_key_from_payload(payload: Mapping[str, Any], *, league: str) -> str:
    value = (
        payload.get("team_key")
        or payload.get("key")
        or payload.get("id")
        or payload.get("abbreviation")
        or payload.get("team")
        or payload.get("team_name")
        or payload.get("name")
    )
    text = str(value or "").strip()
    if not text:
        raise ValueError("Missing required sports team key.")
    return f"{league}:{_slug(text)}"


def game_key_from_payload(payload: Mapping[str, Any], *, league: str) -> str:
    value = payload.get("game_key") or payload.get("key") or payload.get("id")
    if value:
        text = str(value)
        return text if text.startswith(f"{league}:") else f"{league}:{_slug(value)}"
    scheduled = str(payload.get("scheduled_at") or payload.get("date") or "").strip()
    away = str(payload.get("away_team_key") or payload.get("away_team") or "").strip()
    home = str(payload.get("home_team_key") or payload.get("home_team") or "").strip()
    if not scheduled or not away or not home:
        raise ValueError("Missing required sports game key fields.")
    return f"{league}:{_slug(scheduled)}:{_slug(away)}:{_slug(home)}"


def upsert_sports_team(
    session: Session,
    payload: Mapping[str, Any],
    *,
    league: str,
) -> tuple[SportsTeam, bool]:
    normalized_league = normalize_league(league)
    team_key = team_key_from_payload(payload, league=normalized_league)
    now = utc_now()
    team = _pending_team(session, normalized_league, team_key) or session.scalar(
        select(SportsTeam).where(
            SportsTeam.league == normalized_league,
            SportsTeam.team_key == team_key,
        )
    )
    created = team is None
    if team is None:
        team = SportsTeam(
            league=normalized_league,
            team_key=team_key,
            team_name="",
            raw_json="{}",
            created_at=now,
            updated_at=now,
        )
        session.add(team)
    team.team_name = _required_text(
        payload.get("team_name") or payload.get("name") or payload.get("team"),
        "team_name",
    )
    team.abbreviation = _str_or_none(payload.get("abbreviation") or payload.get("abbr"))
    team.city = _str_or_none(payload.get("city"))
    team.conference = _str_or_none(payload.get("conference"))
    team.division = _str_or_none(payload.get("division"))
    team.venue = _str_or_none(payload.get("venue"))
    raw_payload = dict(payload)
    raw_payload["aliases"] = sports_team_aliases_from_payload(raw_payload, team_key=team_key)
    team.raw_json = encode_json(raw_payload)
    team.updated_at = now
    session.flush()
    return team, created


def upsert_sports_game(
    session: Session,
    payload: Mapping[str, Any],
    *,
    league: str,
) -> tuple[SportsGame, bool]:
    normalized_league = normalize_league(league)
    game_key = game_key_from_payload(payload, league=normalized_league)
    now = utc_now()
    game = _pending_game(session, normalized_league, game_key) or session.scalar(
        select(SportsGame).where(
            SportsGame.league == normalized_league,
            SportsGame.game_key == game_key,
        )
    )
    created = game is None
    if game is None:
        game = SportsGame(
            league=normalized_league,
            game_key=game_key,
            status="scheduled",
            home_team_key="",
            away_team_key="",
            raw_json="{}",
            created_at=now,
            updated_at=now,
        )
        session.add(game)
    game.scheduled_at = _parse_sports_datetime(payload, "scheduled_at", "date", "start_time")
    game.season = _str_or_none(payload.get("season"))
    game.status = normalize_sports_status(
        payload.get("status")
        or payload.get("game_status")
        or payload.get("status_type")
        or "scheduled"
    )
    game.home_team_key = _team_key_value(payload, "home", normalized_league)
    game.away_team_key = _team_key_value(payload, "away", normalized_league)
    game.home_score = _int_or_none(payload.get("home_score"))
    game.away_score = _int_or_none(payload.get("away_score"))
    game.venue = _str_or_none(payload.get("venue"))
    game.neutral_site = 1 if _bool(payload.get("neutral_site")) else 0
    game.raw_json = encode_json(dict(payload))
    game.updated_at = now
    session.flush()
    return game, created


def insert_sports_team_stat(
    session: Session,
    payload: Mapping[str, Any],
    *,
    league: str,
) -> SportsTeamStat:
    normalized_league = normalize_league(league)
    row = SportsTeamStat(
        league=normalized_league,
        team_key=_team_key_value(payload, "", normalized_league),
        as_of=_parse_sports_datetime(payload, "as_of", "observed_at", "date") or utc_now(),
        games_played=_int_or_none(payload.get("games_played") or payload.get("games")),
        wins=_int_or_none(payload.get("wins")),
        losses=_int_or_none(payload.get("losses")),
        rating=decimal_to_str(payload.get("rating") or payload.get("strength_rating")),
        offense_rating=decimal_to_str(payload.get("offense_rating")),
        defense_rating=decimal_to_str(payload.get("defense_rating")),
        recent_form=decimal_to_str(payload.get("recent_form")),
        raw_json=encode_json(dict(payload)),
        created_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return row


def insert_sports_injury(
    session: Session,
    payload: Mapping[str, Any],
    *,
    league: str,
) -> SportsInjury:
    normalized_league = normalize_league(league)
    row = SportsInjury(
        league=normalized_league,
        team_key=_team_key_value(payload, "", normalized_league),
        player_name=_required_text(payload.get("player_name") or payload.get("player"), "player"),
        status=_str_or_none(payload.get("status")) or "unknown",
        impact_score=decimal_to_str(payload.get("impact_score") or payload.get("impact")),
        reported_at=_parse_sports_datetime(payload, "reported_at", "observed_at", "date")
        or utc_now(),
        notes=_str_or_none(payload.get("notes")),
        raw_json=encode_json(dict(payload)),
        created_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return row


def insert_sports_odds(
    session: Session,
    payload: Mapping[str, Any],
    *,
    league: str,
) -> SportsOdds:
    normalized_league = normalize_league(league)
    row = SportsOdds(
        league=normalized_league,
        game_key=game_key_from_payload(payload, league=normalized_league),
        sportsbook=_str_or_none(payload.get("sportsbook") or payload.get("book")) or "manual",
        observed_at=_parse_sports_datetime(payload, "observed_at", "as_of", "date")
        or utc_now(),
        home_moneyline=decimal_to_str(payload.get("home_moneyline")),
        away_moneyline=decimal_to_str(payload.get("away_moneyline")),
        spread=decimal_to_str(payload.get("spread")),
        total=decimal_to_str(payload.get("total")),
        home_spread_price=decimal_to_str(payload.get("home_spread_price")),
        away_spread_price=decimal_to_str(payload.get("away_spread_price")),
        over_price=decimal_to_str(payload.get("over_price")),
        under_price=decimal_to_str(payload.get("under_price")),
        raw_json=encode_json(dict(payload)),
        created_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return row


def insert_sports_feature(
    session: Session,
    *,
    league: str,
    game_key: str,
    ticker: str | None,
    home_team_key: str,
    away_team_key: str,
    team_strength_edge: Any,
    injury_edge: Any,
    rest_edge: Any,
    travel_edge: Any,
    odds_edge: Any,
    weather_edge: Any,
    total_edge: Any,
    home_win_probability: Any,
    away_win_probability: Any,
    projected_total: Any,
    confidence_score: Any,
    raw_json: Mapping[str, Any] | None = None,
) -> SportsFeature:
    row = SportsFeature(
        created_at=utc_now(),
        league=normalize_league(league),
        game_key=game_key,
        ticker=ticker,
        home_team_key=home_team_key,
        away_team_key=away_team_key,
        team_strength_edge=decimal_to_str(team_strength_edge) or "0",
        injury_edge=decimal_to_str(injury_edge) or "0",
        rest_edge=decimal_to_str(rest_edge) or "0",
        travel_edge=decimal_to_str(travel_edge) or "0",
        odds_edge=decimal_to_str(odds_edge) or "0",
        weather_edge=decimal_to_str(weather_edge) or "0",
        total_edge=decimal_to_str(total_edge) or "0",
        home_win_probability=decimal_to_str(home_win_probability) or "0.5",
        away_win_probability=decimal_to_str(away_win_probability) or "0.5",
        projected_total=decimal_to_str(projected_total),
        confidence_score=decimal_to_str(confidence_score) or "0",
        raw_json=encode_json(dict(raw_json or {})),
    )
    session.add(row)
    session.flush()
    return row


def insert_sports_market_link(
    session: Session,
    *,
    ticker: str,
    league: str,
    game_key: str,
    market_type: str,
    link_confidence: Any,
    link_reason: str,
    matched_terms: list[str],
    raw_json: Mapping[str, Any] | None = None,
) -> tuple[SportsMarketLink, bool]:
    existing = _pending_market_link(session, ticker, game_key, market_type) or session.scalar(
        select(SportsMarketLink).where(
            SportsMarketLink.ticker == ticker,
            SportsMarketLink.game_key == game_key,
            SportsMarketLink.market_type == market_type,
        )
    )
    if existing is not None:
        return existing, False
    row = SportsMarketLink(
        created_at=utc_now(),
        ticker=ticker,
        league=normalize_league(league),
        game_key=game_key,
        market_type=market_type,
        link_confidence=decimal_to_str(link_confidence) or "0",
        link_reason=link_reason,
        matched_terms_json=encode_json({"matched_terms": matched_terms}),
        raw_json=encode_json(dict(raw_json or {})),
    )
    session.add(row)
    session.flush()
    return row, True


def insert_sports_signal(
    session: Session,
    *,
    ticker: str,
    league: str,
    game_key: str,
    signal_name: str,
    signal_strength: Any,
    signal_direction: str | None,
    confidence: Any,
    explanation: str,
    raw_json: Mapping[str, Any] | None = None,
) -> SportsSignal:
    row = SportsSignal(
        created_at=utc_now(),
        ticker=ticker,
        league=normalize_league(league),
        game_key=game_key,
        signal_name=signal_name,
        signal_strength=decimal_to_str(signal_strength) or "0",
        signal_direction=signal_direction,
        confidence=decimal_to_str(confidence) or "0",
        explanation=explanation,
        raw_json=encode_json(dict(raw_json or {})),
    )
    session.add(row)
    session.flush()
    return row


def sports_teams(
    session: Session,
    *,
    league: str | None = None,
) -> list[SportsTeam]:
    statement = select(SportsTeam)
    if league and normalize_league(league) != "ALL":
        statement = statement.where(SportsTeam.league == normalize_league(league))
    return list(session.scalars(statement.order_by(SportsTeam.league, SportsTeam.team_name)))


def sports_games(
    session: Session,
    *,
    league: str | None = None,
    limit: int | None = None,
) -> list[SportsGame]:
    statement = select(SportsGame)
    if league and normalize_league(league) != "ALL":
        statement = statement.where(SportsGame.league == normalize_league(league))
    statement = statement.order_by(desc(SportsGame.scheduled_at), SportsGame.game_key)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def latest_team_stat(
    session: Session,
    *,
    league: str,
    team_key: str,
) -> SportsTeamStat | None:
    return session.scalar(
        select(SportsTeamStat)
        .where(
            SportsTeamStat.league == normalize_league(league),
            SportsTeamStat.team_key == team_key,
        )
        .order_by(desc(SportsTeamStat.as_of), desc(SportsTeamStat.id))
        .limit(1)
    )


def recent_injuries(
    session: Session,
    *,
    league: str,
    team_key: str,
    lookback_days: int = 30,
) -> list[SportsInjury]:
    cutoff = utc_now() - timedelta(days=lookback_days)
    return list(
        session.scalars(
            select(SportsInjury)
            .where(
                SportsInjury.league == normalize_league(league),
                SportsInjury.team_key == team_key,
                SportsInjury.reported_at >= cutoff,
            )
            .order_by(desc(SportsInjury.reported_at), desc(SportsInjury.id))
        )
    )


def latest_odds(
    session: Session,
    *,
    league: str,
    game_key: str,
) -> SportsOdds | None:
    return session.scalar(
        select(SportsOdds)
        .where(SportsOdds.league == normalize_league(league), SportsOdds.game_key == game_key)
        .order_by(desc(SportsOdds.observed_at), desc(SportsOdds.id))
        .limit(1)
    )


def latest_sports_link(
    session: Session,
    ticker: str,
    *,
    league: str | None = None,
) -> SportsMarketLink | None:
    statement = select(SportsMarketLink).where(SportsMarketLink.ticker == ticker)
    if league and normalize_league(league) != "ALL":
        statement = statement.where(SportsMarketLink.league == normalize_league(league))
    return session.scalar(
        statement.order_by(desc(SportsMarketLink.created_at), desc(SportsMarketLink.id)).limit(1)
    )


def latest_sports_feature(
    session: Session,
    *,
    ticker: str | None = None,
    league: str | None = None,
    game_key: str | None = None,
) -> SportsFeature | None:
    statement = select(SportsFeature)
    if ticker is not None:
        statement = statement.where(SportsFeature.ticker == ticker)
    if league and normalize_league(league) != "ALL":
        statement = statement.where(SportsFeature.league == normalize_league(league))
    if game_key is not None:
        statement = statement.where(SportsFeature.game_key == game_key)
    return session.scalar(
        statement.order_by(desc(SportsFeature.created_at), desc(SportsFeature.id)).limit(1)
    )


def latest_sports_features(
    session: Session,
    *,
    league: str | None = None,
    limit: int | None = None,
) -> list[SportsFeature]:
    statement = select(SportsFeature)
    if league and normalize_league(league) != "ALL":
        statement = statement.where(SportsFeature.league == normalize_league(league))
    rows = list(
        session.scalars(
            statement.order_by(desc(SportsFeature.created_at), desc(SportsFeature.id))
        )
    )
    seen: set[tuple[str, str | None]] = set()
    latest: list[SportsFeature] = []
    for row in rows:
        key = (row.game_key, row.ticker)
        if key in seen:
            continue
        seen.add(key)
        latest.append(row)
        if limit is not None and len(latest) >= limit:
            break
    return latest


def latest_sports_signals_for_ticker(
    session: Session,
    ticker: str,
    *,
    limit: int = 3,
) -> list[SportsSignal]:
    return list(
        session.scalars(
            select(SportsSignal)
            .where(SportsSignal.ticker == ticker)
            .order_by(desc(SportsSignal.created_at), desc(SportsSignal.id))
            .limit(limit)
        )
    )


def recent_sports_signals(
    session: Session,
    *,
    league: str | None = None,
    limit: int = 20,
) -> list[SportsSignal]:
    statement = select(SportsSignal)
    if league and normalize_league(league) != "ALL":
        statement = statement.where(SportsSignal.league == normalize_league(league))
    return list(
        session.scalars(
            statement.order_by(desc(SportsSignal.created_at), desc(SportsSignal.id)).limit(limit)
        )
    )


def sports_market_links(
    session: Session,
    *,
    league: str | None = None,
    game_key: str | None = None,
    limit: int | None = None,
) -> list[SportsMarketLink]:
    statement = select(SportsMarketLink)
    if league and normalize_league(league) != "ALL":
        statement = statement.where(SportsMarketLink.league == normalize_league(league))
    if game_key:
        statement = statement.where(SportsMarketLink.game_key == game_key)
    statement = statement.order_by(desc(SportsMarketLink.created_at), desc(SportsMarketLink.id))
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def sports_game_detail(session: Session, game_key: str) -> dict[str, Any] | None:
    game = session.scalar(select(SportsGame).where(SportsGame.game_key == game_key).limit(1))
    if game is None:
        return None
    links = sports_market_links(session, game_key=game_key)
    feature = latest_sports_feature(session, league=game.league, game_key=game_key)
    return {
        "game": game_row(game),
        "links": [link_row(link) for link in links],
        "feature": feature_row(feature) if feature else None,
        "signals": [signal_row(row) for row in recent_sports_signals(session, limit=20)],
    }


def sports_dashboard_summary(
    session: Session,
    *,
    league: str | None = "ALL",
    limit: int = 20,
) -> dict[str, Any]:
    normalized = normalize_league(league or "ALL")
    games = sports_games(session, league=normalized, limit=limit)
    links = sports_market_links(session, league=normalized, limit=limit)
    signals = recent_sports_signals(session, league=normalized, limit=limit)
    league_counts = Counter(game.league for game in sports_games(session, league="ALL"))
    total_teams = _count(session, SportsTeam, normalized)
    total_games = _count(session, SportsGame, normalized)
    total_features = _count(session, SportsFeature, normalized)
    total_links = _count(session, SportsMarketLink, normalized)
    total_signals = _count(session, SportsSignal, normalized)
    return {
        "summary": {
            "league": normalized,
            "teams": total_teams,
            "games": total_games,
            "features": total_features,
            "links": total_links,
            "signals": total_signals,
            "league_counts": dict(league_counts),
        },
        "latest_games": [game_row(game) for game in games],
        "latest_links": [link_row(link) for link in links],
        "latest_signals": [signal_row(signal) for signal in signals],
    }


def team_row(team: SportsTeam) -> dict[str, Any]:
    return {
        "id": team.id,
        "league": team.league,
        "team_key": team.team_key,
        "team_name": team.team_name,
        "abbreviation": team.abbreviation,
        "city": team.city,
        "aliases": sports_team_aliases(team),
        "conference": team.conference,
        "division": team.division,
        "venue": team.venue,
    }


def game_row(game: SportsGame) -> dict[str, Any]:
    return {
        "id": game.id,
        "league": game.league,
        "game_key": game.game_key,
        "scheduled_at": game.scheduled_at.isoformat() if game.scheduled_at else None,
        "season": game.season,
        "status": game.status,
        "home_team_key": game.home_team_key,
        "away_team_key": game.away_team_key,
        "home_score": game.home_score,
        "away_score": game.away_score,
        "venue": game.venue,
        "neutral_site": bool(game.neutral_site),
    }


def feature_row(feature: SportsFeature) -> dict[str, Any]:
    return {
        "id": feature.id,
        "created_at": feature.created_at.isoformat(),
        "league": feature.league,
        "game_key": feature.game_key,
        "ticker": feature.ticker,
        "home_team_key": feature.home_team_key,
        "away_team_key": feature.away_team_key,
        "team_strength_edge": feature.team_strength_edge,
        "injury_edge": feature.injury_edge,
        "rest_edge": feature.rest_edge,
        "travel_edge": feature.travel_edge,
        "odds_edge": feature.odds_edge,
        "weather_edge": feature.weather_edge,
        "total_edge": feature.total_edge,
        "home_win_probability": feature.home_win_probability,
        "away_win_probability": feature.away_win_probability,
        "projected_total": feature.projected_total,
        "confidence_score": feature.confidence_score,
        "raw": decode_json(feature.raw_json),
    }


def link_row(link: SportsMarketLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "created_at": link.created_at.isoformat(),
        "ticker": link.ticker,
        "league": link.league,
        "game_key": link.game_key,
        "market_type": link.market_type,
        "confidence": link.link_confidence,
        "reason": link.link_reason,
        "matched_terms": decode_json(link.matched_terms_json).get("matched_terms", []),
    }


def signal_row(signal: SportsSignal) -> dict[str, Any]:
    return {
        "id": signal.id,
        "created_at": signal.created_at.isoformat(),
        "ticker": signal.ticker,
        "league": signal.league,
        "game_key": signal.game_key,
        "signal_name": signal.signal_name,
        "signal_strength": signal.signal_strength,
        "signal_direction": signal.signal_direction,
        "confidence": signal.confidence,
        "explanation": signal.explanation,
    }


def _count(session: Session, table: Any, league: str) -> int:
    statement = select(func.count(table.id))
    if league != "ALL":
        statement = statement.where(table.league == league)
    return int(session.scalar(statement) or 0)


def _team_key_value(payload: Mapping[str, Any], side: str, league: str) -> str:
    prefix = f"{side}_" if side else ""
    explicit = payload.get(f"{prefix}team_key")
    if explicit:
        text = str(explicit)
        return text if text.startswith(f"{league}:") else f"{league}:{_slug(text)}"
    name = payload.get(f"{prefix}team") or payload.get(f"{prefix}team_name") or payload.get("team")
    if not name:
        name = payload.get("team_name") or payload.get("name") or payload.get("abbreviation")
    if not name:
        raise ValueError("Missing required sports team key.")
    return f"{league}:{_slug(name)}"


def sports_team_aliases(team: SportsTeam) -> list[str]:
    raw = decode_json(team.raw_json)
    return sports_team_aliases_from_payload(
        {
            **raw,
            "team_key": team.team_key,
            "team_name": team.team_name,
            "abbreviation": team.abbreviation,
            "city": team.city,
        },
        team_key=team.team_key,
    )


def sports_team_aliases_from_payload(
    payload: Mapping[str, Any],
    *,
    team_key: str | None = None,
) -> list[str]:
    aliases: list[str] = []
    explicit = payload.get("aliases")
    if isinstance(explicit, str):
        aliases.extend(part.strip() for part in explicit.split(","))
    elif isinstance(explicit, list):
        aliases.extend(str(part).strip() for part in explicit if part)

    name = str(
        payload.get("team_name")
        or payload.get("display_name")
        or payload.get("displayName")
        or payload.get("name")
        or payload.get("team")
        or ""
    ).strip()
    name_parts = name.split()
    raw_key = str(team_key or payload.get("team_key") or payload.get("key") or "").strip()
    key_label = raw_key.split(":", 1)[-1].replace("-", " ")
    values = [
        raw_key,
        key_label,
        name,
        name_parts[-1] if name_parts else None,
        " ".join(name_parts[-2:]) if len(name_parts) >= 2 else None,
        payload.get("abbreviation") or payload.get("abbr"),
        payload.get("short_name") or payload.get("shortDisplayName"),
        payload.get("nickname"),
        payload.get("city") or payload.get("location"),
    ]
    aliases.extend(str(value).strip() for value in values if value)
    aliases.extend(supplemental_team_aliases(payload, team_key=team_key))
    return _dedupe_aliases(aliases)


def _dedupe_aliases(values: list[str]) -> list[str]:
    aliases: list[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        if len(text) >= 2 and text not in aliases:
            aliases.append(text)
    return aliases


def _parse_sports_datetime(payload: Mapping[str, Any], *keys: str) -> datetime | None:
    value = _first_present(payload, *keys)
    if value is None or value == "":
        return None
    source_timezone = (
        payload.get("source_timezone")
        or payload.get("timezone")
        or payload.get("time_zone")
        or payload.get("tz")
    )
    if isinstance(value, str) and source_timezone and not _datetime_string_has_timezone(value):
        text = value.strip()
        try:
            local = datetime.fromisoformat(text)
            zone = ZoneInfo(str(source_timezone))
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError(f"Invalid sports source timezone/datetime: {value}") from exc
        return local.replace(tzinfo=zone).astimezone(UTC)
    return parse_datetime(value)


def _datetime_string_has_timezone(value: str) -> bool:
    text = value.strip()
    if text.endswith("Z"):
        return True
    if len(text) >= 6 and text[-6] in {"+", "-"} and text[-3] == ":":
        return True
    if len(text) >= 5 and text[-5] in {"+", "-"} and text[-2:].isdigit():
        return True
    return False


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _pending_team(session: Session, league: str, team_key: str) -> SportsTeam | None:
    for item in session.new:
        if isinstance(item, SportsTeam) and item.league == league and item.team_key == team_key:
            return item
    return None


def _pending_game(session: Session, league: str, game_key: str) -> SportsGame | None:
    for item in session.new:
        if isinstance(item, SportsGame) and item.league == league and item.game_key == game_key:
            return item
    return None


def _pending_market_link(
    session: Session,
    ticker: str,
    game_key: str,
    market_type: str,
) -> SportsMarketLink | None:
    for item in session.new:
        if (
            isinstance(item, SportsMarketLink)
            and item.ticker == ticker
            and item.game_key == game_key
            and item.market_type == market_type
        ):
            return item
    return None


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Missing required sports field: {field}")
    return text


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    chars = [char if char.isalnum() else "-" for char in text]
    slug = "-".join(part for part in "".join(chars).split("-") if part)
    return slug or "unknown"


def decimal_or_zero(value: Any):
    return to_decimal(value) or 0
