import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import Market, SportsGame, SportsMarketLink, SportsTeam
from kalshi_predictor.sports.classifier import UNKNOWN, classify_sports_market
from kalshi_predictor.sports.repository import (
    insert_sports_market_link,
    normalize_league,
    sports_games,
    sports_team_aliases,
    sports_teams,
)


@dataclass(frozen=True)
class SportsLinkSummary:
    league: str
    markets_scanned: int
    games_scanned: int
    links_created: int
    market_derived_links: int = 0
    broad_matches_rejected: int = 0
    direct_candidate_links_rejected: int = 0
    links_by_type: dict[str, int] = field(default_factory=dict)
    stopped_early: bool = False


def link_sports_markets(
    session: Session,
    *,
    league: str = "ALL",
    settings: Settings | None = None,
    limit: int | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    progress_every: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> SportsLinkSummary:
    resolved = settings or get_settings()
    normalized = normalize_league(league)
    session.flush()
    statement = select(Market).order_by(Market.ticker)
    if limit is not None:
        statement = statement.limit(limit)
    markets = list(session.scalars(statement))
    games = sports_games(session, league=normalized)
    teams = sports_teams(session, league=normalized)
    team_rows = [_team_mapping(team) for team in teams]
    team_by_key = {team.team_key: team for team in teams}
    created = 0
    market_derived = 0
    broad_rejected = 0
    direct_rejected = 0
    by_type: dict[str, int] = {}

    stopped_early = False
    for index, market in enumerate(markets, start=1):
        if should_stop is not None and should_stop():
            stopped_early = True
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=index - 1,
                total=len(markets),
                ticker=market.ticker,
                status="STOPPED_EARLY",
                created=created,
                market_derived=market_derived,
            )
            break
        classification = classify_sports_market(market, teams=team_rows)
        if classification["league"] == UNKNOWN and normalized == "ALL":
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=index,
                total=len(markets),
                ticker=market.ticker,
                status="SKIPPED_UNKNOWN_LEAGUE",
                created=created,
                market_derived=market_derived,
            )
            continue
        direct_candidates: list[tuple[SportsGame, Decimal, str, list[str], str]] = []
        for game in games:
            confidence, reason, matched_terms, market_type = score_sports_market_link(
                market,
                game,
                home_team=team_by_key.get(game.home_team_key),
                away_team=team_by_key.get(game.away_team_key),
                classification=classification,
            )
            if confidence < resolved.sports_min_link_confidence:
                continue
            if resolved.sports_require_specific_game_match and not _has_specific_game_match(
                matched_terms,
                game,
                home_team=team_by_key.get(game.home_team_key),
                away_team=team_by_key.get(game.away_team_key),
            ):
                continue
            direct_candidates.append((game, confidence, reason, matched_terms, market_type))

        max_direct_links = max(1, resolved.sports_max_direct_links_per_market)
        if len(direct_candidates) > max_direct_links:
            broad_rejected += 1
            direct_rejected += len(direct_candidates)
            fallback = _market_derived_link(
                session,
                market,
                classification=classification,
                requested_league=normalized,
                min_confidence=resolved.sports_min_link_confidence,
                link_reason=(
                    "Broad sports match rejected: market matched "
                    f"{len(direct_candidates)} games, above max "
                    f"{max_direct_links}. Use verified component provenance before "
                    "learning or paper trading this market."
                ),
                raw_extra={
                    "source": "broad-match-quarantine",
                    "direct_candidates_rejected": len(direct_candidates),
                    "max_direct_links_per_market": max_direct_links,
                },
            )
            if fallback is not None:
                created += 1
                market_derived += 1
                by_type[fallback.market_type] = by_type.get(fallback.market_type, 0) + 1
        for game, confidence, reason, matched_terms, market_type in (
            direct_candidates if len(direct_candidates) <= max_direct_links else []
        ):
            _, was_created = insert_sports_market_link(
                session,
                ticker=market.ticker,
                league=game.league,
                game_key=game.game_key,
                market_type=market_type,
                link_confidence=confidence,
                link_reason=reason,
                matched_terms=matched_terms,
                raw_json={
                    "market_title": market.title,
                    "market_ticker": market.ticker,
                    "game_key": game.game_key,
                    "classification": classification,
                },
            )
            if was_created:
                created += 1
                by_type[market_type] = by_type.get(market_type, 0) + 1
        if not direct_candidates:
            fallback = _market_derived_link(
                session,
                market,
                classification=classification,
                requested_league=normalized,
                min_confidence=resolved.sports_min_link_confidence,
            )
            if fallback is not None:
                created += 1
                market_derived += 1
                by_type[fallback.market_type] = by_type.get(fallback.market_type, 0) + 1
        _emit_progress(
            progress_callback,
            progress_every=progress_every,
            processed=index,
            total=len(markets),
            ticker=market.ticker,
            status="PROGRESS",
            created=created,
            market_derived=market_derived,
        )

    return SportsLinkSummary(
        league=normalized,
        markets_scanned=len(markets),
        games_scanned=len(games),
        links_created=created,
        market_derived_links=market_derived,
        broad_matches_rejected=broad_rejected,
        direct_candidate_links_rejected=direct_rejected,
        links_by_type=by_type,
        stopped_early=stopped_early,
    )


def score_sports_market_link(
    market: Market,
    game: SportsGame,
    *,
    home_team: SportsTeam | None,
    away_team: SportsTeam | None,
    classification: dict[str, object] | None = None,
) -> tuple[Decimal, str, list[str], str]:
    resolved_classification = classification or classify_sports_market(market)
    text = str(resolved_classification.get("text") or "").lower()
    market_type = str(resolved_classification.get("market_type") or UNKNOWN)
    score = Decimal("0")
    matched: set[str] = set()

    if resolved_classification.get("league") == game.league:
        score += Decimal("0.20")
        matched.add(game.league)

    for label, team in (("home", home_team), ("away", away_team)):
        aliases = _team_aliases(team, fallback_key=getattr(game, f"{label}_team_key"))
        hits = [alias for alias in aliases if alias and alias in text]
        if hits:
            score += Decimal("0.25")
            matched.update(hits[:3])

    if market_type != UNKNOWN:
        score += Decimal("0.10")
        matched.add(market_type.lower())

    if _close_to_game_time(market, game):
        score += Decimal("0.15")
        matched.add("game_time")

    if home_team is not None and away_team is not None and home_team.team_name.lower() in text:
        score += Decimal("0.05")
    if home_team is not None and away_team is not None and away_team.team_name.lower() in text:
        score += Decimal("0.05")

    confidence = min(score, Decimal("1.00")).quantize(Decimal("0.0001"))
    if not matched:
        return Decimal("0"), "No sports game terms matched.", [], market_type
    reason = f"{game.league} market matched {len(matched)} game term(s)."
    return confidence, reason, sorted(matched), market_type


def _close_to_game_time(market: Market, game: SportsGame) -> bool:
    if market.close_time is None or game.scheduled_at is None:
        return False
    market_close = market.close_time
    scheduled = game.scheduled_at
    if (market_close.tzinfo is None) != (scheduled.tzinfo is None):
        market_close = market_close.replace(tzinfo=None)
        scheduled = scheduled.replace(tzinfo=None)
    delta = abs(market_close - scheduled)
    return delta <= timedelta(days=3)


def _team_mapping(team: SportsTeam) -> dict[str, object]:
    return {
        "league": team.league,
        "team_key": team.team_key,
        "team_name": team.team_name,
        "abbreviation": team.abbreviation,
        "city": team.city,
        "aliases": sports_team_aliases(team),
    }


def _team_aliases(team: SportsTeam | None, *, fallback_key: str) -> list[str]:
    aliases = [fallback_key.split(":", 1)[-1].replace("-", " ")]
    if team is not None:
        aliases.extend(sports_team_aliases(team))
    deduped: list[str] = []
    for alias in aliases:
        text = str(alias or "").strip().lower()
        if len(text) >= 2 and text not in deduped:
            deduped.append(text)
    return deduped


def _has_specific_game_match(
    matched_terms: list[str],
    game: SportsGame,
    *,
    home_team: SportsTeam | None,
    away_team: SportsTeam | None,
) -> bool:
    matched = {str(term or "").strip().lower() for term in matched_terms}
    generic = {game.league.lower(), "game_time"}
    for market_type in ("moneyline", "spread", "total", "player_prop", "unknown"):
        generic.add(market_type)
    for label, team in (("home", home_team), ("away", away_team)):
        for alias in _team_aliases(team, fallback_key=getattr(game, f"{label}_team_key")):
            normalized = alias.strip().lower()
            if len(normalized) >= 3 and normalized not in generic and normalized in matched:
                return True
    return False


def _market_derived_link(
    session: Session,
    market: Market,
    *,
    classification: dict[str, object],
    requested_league: str,
    min_confidence: Decimal,
    link_reason: str | None = None,
    raw_extra: dict[str, object] | None = None,
) -> SportsMarketLink | None:
    league = str(classification.get("league") or UNKNOWN).upper()
    market_type = str(classification.get("market_type") or UNKNOWN)
    if league == UNKNOWN or market_type == UNKNOWN:
        return None
    if requested_league != "ALL" and requested_league != league:
        return None
    confidence = Decimal("0.50")
    if confidence < min_confidence:
        return None
    matched_terms = [league.lower(), market_type.lower(), "market_derived"]
    row, was_created = insert_sports_market_link(
        session,
        ticker=market.ticker,
        league=league,
        game_key=f"{league}:market-derived:{_slug(market.ticker)}",
        market_type=market_type,
        link_confidence=confidence,
        link_reason=link_reason
        or (
            "Market text names a supported sports league, but no matching ingested game "
            "was found. Ingest sports schedule/team data to upgrade this link."
        ),
        matched_terms=matched_terms,
        raw_json={
            "market_title": market.title,
            "market_ticker": market.ticker,
            "classification": classification,
            "phase": "3Y",
            "source": "market-derived-fallback",
            **dict(raw_extra or {}),
        },
    )
    return row if was_created else None


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "unknown"


def _emit_progress(
    progress_callback: Callable[[dict[str, object]], None] | None,
    *,
    progress_every: int,
    processed: int,
    total: int,
    ticker: str,
    status: str,
    created: int,
    market_derived: int,
) -> None:
    if progress_callback is None:
        return
    cadence = max(progress_every, 0)
    if status == "PROGRESS" and cadence and processed % cadence != 0 and processed != total:
        return
    progress_callback(
        {
            "stage": "sports_link",
            "processed": processed,
            "total": total,
            "ticker": ticker,
            "status": status,
            "created": created,
            "market_derived": market_derived,
        }
    )
