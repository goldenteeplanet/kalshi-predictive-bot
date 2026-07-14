from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market, MarketLeg, SportsGame, SportsMarketLink, SportsTeam
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3af import DEFAULT_SOCCER_COMPETITIONS
from kalshi_predictor.sports.repository import sports_team_aliases
from kalshi_predictor.utils.time import utc_now

PHASE_3AG_VERSION = "phase3ag_v1"
VERIFIED_SOURCE = "verified_schedule"
PARTIAL_SOURCE = "partial_market_derived"
DEFAULT_MAX_SCHEDULE_DELTA_HOURS = 18
MANUAL_TEMPLATE_SOURCE = "manual_verified_schedule_template"

GENERIC_SOCCER_ENTITY_FRAGMENTS = (
    "both teams",
    "goals scored",
    "goal scored",
    "wins by",
    "over ",
    "under ",
    "runs scored",
    "run scored",
    "points scored",
    "point scored",
)


@dataclass(frozen=True)
class Phase3AGArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    manual_template_path: Path


@dataclass(frozen=True)
class Phase3AGRepairArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    alias_candidates_path: Path
    manual_candidates_path: Path


def build_sports_ambiguity_coverage(
    session: Session,
    *,
    manual_template_path: Path = Path("data/sports_schedules/soccer_verified_manual_template.json"),
    max_schedule_delta_hours: int = DEFAULT_MAX_SCHEDULE_DELTA_HOURS,
    write_manual_template: bool = True,
) -> dict[str, Any]:
    """Build paper-only diagnostics for sports ambiguity and soccer schedule coverage."""
    session.flush()
    links = list(session.scalars(select(SportsMarketLink).order_by(SportsMarketLink.id)))
    partial_links = [link for link in links if _link_provenance(link) == PARTIAL_SOURCE]
    verified_links = [link for link in links if _link_provenance(link) == VERIFIED_SOURCE]
    games = list(
        session.scalars(select(SportsGame).order_by(SportsGame.league, SportsGame.game_key))
    )
    teams = list(
        session.scalars(select(SportsTeam).order_by(SportsTeam.league, SportsTeam.team_key))
    )

    soccer_partials = [link for link in partial_links if link.league == "SOCCER"]
    soccer_games = [game for game in games if game.league == "SOCCER" and _game_is_verified(game)]
    soccer_entities = _soccer_entities(session, soccer_partials)
    suspect_verified = _suspect_verified_links(
        session,
        verified_links,
        games={game.game_key: game for game in games},
        teams=teams,
        max_schedule_delta_hours=max_schedule_delta_hours,
    )
    manual_template = _manual_template_payload(soccer_entities)
    if write_manual_template:
        manual_template_path.parent.mkdir(parents=True, exist_ok=True)
        manual_template_path.write_text(
            json.dumps(manual_template, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AG",
        "phase_version": PHASE_3AG_VERSION,
        "mode": "PAPER_ONLY_SPORTS_AMBIGUITY_SOCCER_COVERAGE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "max_schedule_delta_hours": max_schedule_delta_hours,
        "summary": {
            "sports_links": len(links),
            "partial_links": len(partial_links),
            "verified_links": len(verified_links),
            "soccer_partial_links": len(soccer_partials),
            "verified_soccer_games": len(soccer_games),
            "soccer_entities_detected": len(soccer_entities),
            "suspect_verified_links": len(suspect_verified),
            "manual_template_written": write_manual_template,
        },
        "league_breakdown": _league_breakdown(partial_links, verified_links, games),
        "soccer_coverage": {
            "status": "NEEDS_MANUAL_OR_ADDITIONAL_COMPETITION_DATA"
            if soccer_partials and not soccer_games
            else "HAS_VERIFIED_GAMES",
            "recommended_competitions": list(DEFAULT_SOCCER_COMPETITIONS),
            "manual_template_path": str(manual_template_path),
            "top_entities": soccer_entities[:30],
        },
        "ambiguity_diagnostics": {
            "suspect_verified_links": suspect_verified[:50],
            "guardrails": [
                "Phase 3AE now rejects verified matches outside the schedule delta window.",
                (
                    "Phase 3AE now rejects single-game verified links when market text "
                    "mentions conflicting teams."
                ),
                "Existing suspect verified links are reported here, not deleted automatically.",
            ],
        },
        "recommended_next_action": _next_action(
            soccer_partials=len(soccer_partials),
            soccer_games=len(soccer_games),
            suspect_verified=len(suspect_verified),
        ),
    }


def write_phase3ag_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ag"),
    manual_template_path: Path = Path("data/sports_schedules/soccer_verified_manual_template.json"),
    max_schedule_delta_hours: int = DEFAULT_MAX_SCHEDULE_DELTA_HOURS,
    write_manual_template: bool = True,
) -> Phase3AGArtifactSet:
    payload = build_sports_ambiguity_coverage(
        session,
        manual_template_path=manual_template_path,
        max_schedule_delta_hours=max_schedule_delta_hours,
        write_manual_template=write_manual_template,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ag_sports_ambiguity_coverage.json"
    markdown_path = output_dir / "phase3ag_sports_ambiguity_coverage.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AGArtifactSet(output_dir, json_path, markdown_path, manual_template_path)


def build_sports_link_repair_pass(
    session: Session,
    *,
    phase3ae_path: Path = Path("reports/phase3ae/phase3ae_verified_sports_connector.json"),
    limit: int | None = None,
    max_schedule_delta_hours: int = DEFAULT_MAX_SCHEDULE_DELTA_HOURS,
) -> dict[str, Any]:
    """Analyze Phase 3AE NO_VERIFIED_MATCH rows without upgrading links."""
    session.flush()
    source_payload = _load_json_file(phase3ae_path)
    failure_rows = [
        row
        for row in source_payload.get("rows", [])
        if str(row.get("status") or "").upper() == "NO_VERIFIED_MATCH"
    ]
    if limit is not None:
        failure_rows = failure_rows[:limit]
    tickers = sorted({str(row.get("ticker") or "") for row in failure_rows if row.get("ticker")})
    markets = _markets_by_ticker(session, tickers)
    legs_by_ticker = _sports_legs_by_ticker(session, tickers)
    teams = list(
        session.scalars(select(SportsTeam).order_by(SportsTeam.league, SportsTeam.team_key))
    )
    games = [
        game
        for game in session.scalars(
            select(SportsGame).order_by(SportsGame.league, SportsGame.game_key)
        )
        if _game_is_verified(game)
    ]
    teams_by_key = {team.team_key: team for team in teams}
    team_aliases = _team_alias_index(teams)
    games_by_league = _games_by_league(games)

    rows: list[dict[str, Any]] = []
    cause_counts: Counter[str] = Counter()
    alias_counter: dict[tuple[str, str, str], dict[str, Any]] = {}
    manual_candidates: list[dict[str, Any]] = []
    clean_candidates = 0

    for failure in failure_rows:
        row = _repair_row(
            failure,
            market=markets.get(str(failure.get("ticker") or "")),
            legs=legs_by_ticker.get(str(failure.get("ticker") or ""), []),
            games_by_league=games_by_league,
            teams_by_key=teams_by_key,
            team_aliases=team_aliases,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
        rows.append(row)
        cause_counts[row["primary_cause"]] += 1
        if row["clean_candidate_count"]:
            clean_candidates += 1
        for alias in row["missing_alias_candidates"]:
            key = (row["league"], alias["entity"], alias["entity_role"])
            existing = alias_counter.setdefault(
                key,
                {
                    "league": row["league"],
                    "entity": alias["entity"],
                    "entity_role": alias["entity_role"],
                    "count": 0,
                    "example_tickers": [],
                    "suggested_action": alias["suggested_action"],
                },
            )
            existing["count"] += 1
            if row["ticker"] not in existing["example_tickers"]:
                existing["example_tickers"].append(row["ticker"])
                existing["example_tickers"] = existing["example_tickers"][:5]
        manual_candidates.append(_manual_candidate(row))

    alias_candidates = sorted(
        alias_counter.values(),
        key=lambda item: (-int(item["count"]), item["league"], item["entity"]),
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AG",
        "phase_version": PHASE_3AG_VERSION,
        "mode": "PAPER_ONLY_SPORTS_LINK_REPAIR_PASS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "phase3ae_source_path": str(phase3ae_path),
        "max_schedule_delta_hours": max_schedule_delta_hours,
        "auto_upgrade_policy": {
            "auto_upgrades_created": 0,
            "policy": (
                "Report-only. Do not create verified sports links unless team, time, "
                "and market type are clean; Phase 3AE remains the upgrade path."
            ),
        },
        "summary": {
            "phase3ae_no_verified_match_rows": len(failure_rows),
            "failed_markets_reviewed": len(tickers),
            "clean_manual_candidates": clean_candidates,
            "alias_candidates": len(alias_candidates),
            "manual_candidate_rows": len(manual_candidates),
            "auto_upgrades_created": 0,
        },
        "cause_breakdown": [
            {"cause": cause, "count": count} for cause, count in cause_counts.most_common()
        ],
        "missing_alias_candidates": alias_candidates,
        "manual_disambiguation_candidates": manual_candidates,
        "rows": rows,
        "recommended_next_action": _repair_next_action(cause_counts, alias_candidates),
    }


def write_phase3ag_repair_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ag"),
    phase3ae_path: Path = Path("reports/phase3ae/phase3ae_verified_sports_connector.json"),
    limit: int | None = None,
    max_schedule_delta_hours: int = DEFAULT_MAX_SCHEDULE_DELTA_HOURS,
) -> Phase3AGRepairArtifactSet:
    payload = build_sports_link_repair_pass(
        session,
        phase3ae_path=phase3ae_path,
        limit=limit,
        max_schedule_delta_hours=max_schedule_delta_hours,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ag_sports_link_repair_pass.json"
    markdown_path = output_dir / "phase3ag_sports_link_repair_pass.md"
    alias_path = output_dir / "phase3ag_missing_alias_candidates.json"
    manual_path = output_dir / "phase3ag_manual_disambiguation_candidates.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_repair_markdown(payload), encoding="utf-8")
    alias_path.write_text(
        json.dumps(payload["missing_alias_candidates"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    manual_path.write_text(
        json.dumps(
            payload["manual_disambiguation_candidates"],
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    return Phase3AGRepairArtifactSet(output_dir, json_path, markdown_path, alias_path, manual_path)


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"rows": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _markets_by_ticker(session: Session, tickers: list[str]) -> dict[str, Market]:
    if not tickers:
        return {}
    return {
        market.ticker: market
        for market in session.scalars(select(Market).where(Market.ticker.in_(tickers)))
    }


def _sports_legs_by_ticker(session: Session, tickers: list[str]) -> dict[str, list[MarketLeg]]:
    if not tickers:
        return {}
    grouped: dict[str, list[MarketLeg]] = defaultdict(list)
    rows = session.scalars(
        select(MarketLeg)
        .where(MarketLeg.ticker.in_(tickers), MarketLeg.category == "sports")
        .order_by(MarketLeg.ticker, MarketLeg.leg_index)
    )
    for row in rows:
        grouped[row.ticker].append(row)
    return dict(grouped)


def _team_alias_index(teams: list[SportsTeam]) -> list[tuple[SportsTeam, list[str]]]:
    return [(team, _team_aliases(team)) for team in teams]


def _games_by_league(games: list[SportsGame]) -> dict[str, list[SportsGame]]:
    grouped: dict[str, list[SportsGame]] = defaultdict(list)
    for game in games:
        grouped[str(game.league or "UNKNOWN").upper()].append(game)
    return dict(grouped)


def _repair_row(
    failure: dict[str, Any],
    *,
    market: Market | None,
    legs: list[MarketLeg],
    games_by_league: dict[str, list[SportsGame]],
    teams_by_key: dict[str, SportsTeam],
    team_aliases: list[tuple[SportsTeam, list[str]]],
    max_schedule_delta_hours: int,
) -> dict[str, Any]:
    ticker = str(failure.get("ticker") or "")
    league = _normalized_repair_value(failure.get("league"), default="UNKNOWN")
    market_type = _normalized_repair_value(failure.get("market_type"), default="UNKNOWN")
    candidate_games = _repair_candidate_games(league, games_by_league)
    window_games = [
        game
        for game in candidate_games
        if market is not None
        and _schedule_window_allowed_for_repair(
            market,
            game,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
    ]
    text = _repair_market_text(market, failure=failure, legs=legs)
    entities = _repair_entities(legs)
    matched_team_keys = _matched_team_keys(text, team_aliases, league=league)
    unmatched_entities = [
        entity
        for entity in entities
        if not _entity_matches_team_alias(entity, team_aliases, league=league)
    ]
    missing_alias_candidates = [
        _missing_alias_candidate(entity, market_type=market_type)
        for entity in unmatched_entities
        if _is_missing_alias_candidate(entity)
    ]
    game_candidates = [
        _game_candidate(
            market,
            game,
            teams_by_key=teams_by_key,
            matched_team_keys=matched_team_keys,
            market_type=market_type,
            unmatched_entities=unmatched_entities,
        )
        for game in window_games
    ]
    clean_candidates = [candidate for candidate in game_candidates if candidate["clean"]]
    primary_cause = _repair_primary_cause(
        ticker=ticker,
        market=market,
        market_type=market_type,
        entities=entities,
        missing_alias_candidates=missing_alias_candidates,
        candidate_games=candidate_games,
        window_games=window_games,
        clean_candidates=clean_candidates,
    )
    return {
        "ticker": ticker,
        "league": league,
        "market_type": market_type,
        "phase3ae_status": failure.get("status"),
        "phase3ae_reason": failure.get("reason"),
        "primary_cause": primary_cause,
        "market_title": market.title if market else None,
        "market_status": market.status if market else None,
        "market_close_time": (
            market.close_time.isoformat() if market and market.close_time else None
        ),
        "partial_game_key": failure.get("partial_game_key"),
        "entities": entities,
        "matched_teams": [
            _team_label(teams_by_key.get(team_key), team_key)
            for team_key in sorted(matched_team_keys)
        ],
        "unmatched_entities": unmatched_entities,
        "missing_alias_candidates": missing_alias_candidates,
        "verified_games_for_league": len(candidate_games),
        "verified_games_in_time_window": len(window_games),
        "game_candidates": game_candidates[:10],
        "clean_candidate_count": len(clean_candidates),
        "safe_action": _safe_repair_action(primary_cause),
    }


def _normalized_repair_value(value: object, *, default: str) -> str:
    text = str(value or default).strip().upper()
    return text or default


def _repair_candidate_games(
    league: str,
    games_by_league: dict[str, list[SportsGame]],
) -> list[SportsGame]:
    if league in {"", "ALL", "SPORTS", "UNKNOWN"}:
        return [game for games in games_by_league.values() for game in games]
    return games_by_league.get(league, [])


def _schedule_window_allowed_for_repair(
    market: Market,
    game: SportsGame,
    *,
    max_schedule_delta_hours: int,
) -> bool:
    if max_schedule_delta_hours <= 0:
        return True
    if market.close_time is None or game.scheduled_at is None:
        return False
    market_close = market.close_time
    scheduled = game.scheduled_at
    if (market_close.tzinfo is None) != (scheduled.tzinfo is None):
        market_close = market_close.replace(tzinfo=None)
        scheduled = scheduled.replace(tzinfo=None)
    return abs(market_close - scheduled) <= timedelta(hours=max_schedule_delta_hours)


def _repair_market_text(
    market: Market | None,
    *,
    failure: dict[str, Any],
    legs: list[MarketLeg],
) -> str:
    parts: list[object] = [
        failure.get("ticker"),
        failure.get("league"),
        failure.get("market_type"),
        failure.get("partial_game_key"),
    ]
    if market is not None:
        parts.extend(
            [
                market.ticker,
                market.title,
                market.subtitle,
                market.event_ticker,
                market.series_ticker,
                market.rules_primary,
                market.rules_secondary,
            ]
        )
    for leg in legs:
        parts.extend([leg.raw_text, leg.entity_name, leg.market_type])
    return " ".join(str(part or "") for part in parts).lower()


def _repair_entities(legs: list[MarketLeg]) -> list[str]:
    entities: list[str] = []
    for leg in legs:
        entity = _clean_entity(leg.entity_name or leg.raw_text)
        if entity and entity not in entities:
            entities.append(entity)
    return entities


def _matched_team_keys(
    text: str,
    team_aliases: list[tuple[SportsTeam, list[str]]],
    *,
    league: str,
) -> set[str]:
    return {
        team.team_key
        for team, aliases in team_aliases
        if _team_in_repair_league(team, league)
        and any(alias and _alias_in_text(text, alias) for alias in aliases)
    }


def _entity_matches_team_alias(
    entity: str,
    team_aliases: list[tuple[SportsTeam, list[str]]],
    *,
    league: str,
) -> bool:
    text = entity.lower()
    return any(
        _team_in_repair_league(team, league)
        and any(alias and _alias_in_text(text, alias) for alias in aliases)
        for team, aliases in team_aliases
    )


def _team_in_repair_league(team: SportsTeam, league: str) -> bool:
    return league in {"", "ALL", "SPORTS", "UNKNOWN"} or team.league == league


def _is_missing_alias_candidate(entity: str) -> bool:
    lowered = entity.lower()
    if len(lowered) < 3:
        return False
    if lowered in {"tie", "yes", "no"}:
        return False
    if any(fragment in lowered for fragment in GENERIC_SOCCER_ENTITY_FRAGMENTS):
        return False
    return True


def _missing_alias_candidate(entity: str, *, market_type: str) -> dict[str, Any]:
    role = "player_or_participant_alias" if market_type == "PLAYER_PROP" else "team_or_entity_alias"
    return {
        "entity": entity,
        "entity_role": role,
        "suggested_action": (
            "Verify this name against an official team/player/participant source before "
            "adding it as an alias or roster mapping."
        ),
    }


def _game_candidate(
    market: Market | None,
    game: SportsGame,
    *,
    teams_by_key: dict[str, SportsTeam],
    matched_team_keys: set[str],
    market_type: str,
    unmatched_entities: list[str],
) -> dict[str, Any]:
    game_team_keys = {game.home_team_key, game.away_team_key}
    matched_game_keys = sorted(matched_team_keys & game_team_keys)
    clean = (
        market is not None
        and market.close_time is not None
        and game.scheduled_at is not None
        and market_type not in {"", "UNKNOWN"}
        and len(matched_game_keys) == 2
        and not unmatched_entities
    )
    return {
        "game_key": game.game_key,
        "league": game.league,
        "scheduled_at": game.scheduled_at.isoformat() if game.scheduled_at else None,
        "home_team": _team_label(teams_by_key.get(game.home_team_key), game.home_team_key),
        "away_team": _team_label(teams_by_key.get(game.away_team_key), game.away_team_key),
        "matched_game_teams": [
            _team_label(teams_by_key.get(team_key), team_key) for team_key in matched_game_keys
        ],
        "team_match_count": len(matched_game_keys),
        "time_delta_hours": _time_delta_hours(market, game),
        "market_type_clean": market_type not in {"", "UNKNOWN"},
        "clean": clean,
    }


def _time_delta_hours(market: Market | None, game: SportsGame) -> float | None:
    if market is None or market.close_time is None or game.scheduled_at is None:
        return None
    market_close = market.close_time
    scheduled = game.scheduled_at
    if (market_close.tzinfo is None) != (scheduled.tzinfo is None):
        market_close = market_close.replace(tzinfo=None)
        scheduled = scheduled.replace(tzinfo=None)
    return round(abs((market_close - scheduled).total_seconds()) / 3600, 2)


def _repair_primary_cause(
    *,
    ticker: str,
    market: Market | None,
    market_type: str,
    entities: list[str],
    missing_alias_candidates: list[dict[str, Any]],
    candidate_games: list[SportsGame],
    window_games: list[SportsGame],
    clean_candidates: list[dict[str, Any]],
) -> str:
    if market is None:
        return "MISSING_MARKET_ROW"
    if not candidate_games:
        return "NO_VERIFIED_GAMES_FOR_LEAGUE"
    if market.close_time is None:
        return "MARKET_CLOSE_TIME_MISSING"
    if not window_games:
        return "NO_VERIFIED_GAMES_IN_SCHEDULE_WINDOW"
    if len(entities) > 2 or "MULTIGAME" in ticker.upper():
        return "MULTI_LEG_MARKET_REQUIRES_MANUAL_DISAMBIGUATION"
    if not entities:
        return "NO_PARSED_SPORTS_ENTITIES"
    if market_type in {"", "UNKNOWN"}:
        return "MARKET_TYPE_NOT_CLEAN"
    if missing_alias_candidates and market_type == "PLAYER_PROP":
        return "PLAYER_PROP_NEEDS_PLAYER_TEAM_MAPPING"
    if missing_alias_candidates:
        return "MISSING_TEAM_OR_PLAYER_ALIAS"
    if len(clean_candidates) > 1:
        return "AMBIGUOUS_MULTIPLE_CLEAN_CANDIDATES"
    if len(clean_candidates) == 1:
        return "CLEAN_MANUAL_CANDIDATE_REPORT_ONLY"
    return "INSUFFICIENT_TEAM_TIME_TYPE_MATCH"


def _safe_repair_action(cause: str) -> str:
    if cause == "CLEAN_MANUAL_CANDIDATE_REPORT_ONLY":
        return "Manual review only; rerun Phase 3AE for any actual verified link creation."
    return "Do not auto-upgrade; repair aliases, schedules, or disambiguation evidence first."


def _manual_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": row["ticker"],
        "league": row["league"],
        "market_type": row["market_type"],
        "primary_cause": row["primary_cause"],
        "market_title": row["market_title"],
        "market_close_time": row["market_close_time"],
        "entities": row["entities"],
        "unmatched_entities": row["unmatched_entities"],
        "candidate_games": row["game_candidates"][:5],
        "clean_candidate_count": row["clean_candidate_count"],
        "safe_action": row["safe_action"],
    }


def _repair_next_action(
    cause_counts: Counter[str],
    alias_candidates: list[dict[str, Any]],
) -> str:
    if alias_candidates:
        return (
            "Review phase3ag_missing_alias_candidates.json, add only verified "
            "team/player aliases or roster mappings, then rerun Phase 3AE."
        )
    if cause_counts.get("NO_VERIFIED_GAMES_IN_SCHEDULE_WINDOW"):
        return "Expand verified schedules for the failed close-time windows, then rerun Phase 3AE."
    if cause_counts.get("MULTI_LEG_MARKET_REQUIRES_MANUAL_DISAMBIGUATION"):
        return "Manually disambiguate multi-leg sports markets before any verified upgrades."
    return "Review manual disambiguation candidates, then rerun Phase 3AE."


def _soccer_entities(
    session: Session,
    soccer_partials: list[SportsMarketLink],
) -> list[dict[str, Any]]:
    tickers = {link.ticker for link in soccer_partials}
    if not tickers:
        return []
    rows = list(
        session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker.in_(tickers), MarketLeg.category == "sports")
            .order_by(MarketLeg.ticker, MarketLeg.leg_index)
        )
    )
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for row in rows:
        entity = _clean_entity(row.entity_name or row.raw_text)
        if not entity:
            continue
        counts[entity] += 1
        examples.setdefault(entity, row.ticker)
    return [
        {"entity": entity, "count": count, "example_ticker": examples.get(entity)}
        for entity, count in counts.most_common()
    ]


def _clean_entity(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"tie", "yes", "no"}:
        return None
    if any(fragment in lowered for fragment in GENERIC_SOCCER_ENTITY_FRAGMENTS):
        return None
    return text


def _suspect_verified_links(
    session: Session,
    links: list[SportsMarketLink],
    *,
    games: dict[str, SportsGame],
    teams: list[SportsTeam],
    max_schedule_delta_hours: int,
) -> list[dict[str, Any]]:
    by_key = {team.team_key: team for team in teams}
    rows: list[dict[str, Any]] = []
    for link in links:
        market = session.get(Market, link.ticker)
        game = games.get(link.game_key)
        if market is None or game is None:
            continue
        reasons: list[str] = []
        if _schedule_delta_too_wide(market, game, max_schedule_delta_hours):
            reasons.append("schedule_delta_too_wide")
        mentioned = _mentioned_team_keys(market, teams)
        candidate_keys = {game.home_team_key, game.away_team_key}
        conflict_keys = sorted(mentioned - candidate_keys)
        if conflict_keys:
            reasons.append("conflicting_team_mentions")
        if not reasons:
            continue
        rows.append(
            {
                "ticker": link.ticker,
                "league": link.league,
                "game_key": link.game_key,
                "market_status": market.status,
                "market_close_time": market.close_time.isoformat() if market.close_time else None,
                "game_scheduled_at": game.scheduled_at.isoformat() if game.scheduled_at else None,
                "reasons": reasons,
                "conflicting_teams": [
                    _team_label(by_key.get(team_key), team_key) for team_key in conflict_keys
                ],
            }
        )
    return rows


def _schedule_delta_too_wide(
    market: Market,
    game: SportsGame,
    max_schedule_delta_hours: int,
) -> bool:
    if max_schedule_delta_hours <= 0:
        return False
    if market.close_time is None or game.scheduled_at is None:
        return False
    market_close = market.close_time
    scheduled = game.scheduled_at
    if (market_close.tzinfo is None) != (scheduled.tzinfo is None):
        market_close = market_close.replace(tzinfo=None)
        scheduled = scheduled.replace(tzinfo=None)
    return abs(market_close - scheduled) > timedelta(hours=max_schedule_delta_hours)


def _mentioned_team_keys(market: Market, teams: list[SportsTeam]) -> set[str]:
    text = " ".join(
        str(part or "")
        for part in (
            market.ticker,
            market.title,
            market.subtitle,
            market.event_ticker,
            market.series_ticker,
            market.rules_primary,
            market.rules_secondary,
        )
    ).lower()
    return {
        team.team_key
        for team in teams
        if any(_alias_in_text(text, alias) for alias in _team_aliases(team))
    }


def _team_aliases(team: SportsTeam) -> list[str]:
    return [alias for alias in sports_team_aliases(team) if len(alias) >= 3]


def _alias_in_text(text: str, alias: str) -> bool:
    if " " in alias:
        return alias in text
    padded = f" {text} "
    return any(f"{sep}{alias}{end}" in padded for sep in (" ", "-", ",") for end in (" ", ",", "."))


def _team_label(team: SportsTeam | None, team_key: str) -> str:
    if team is None:
        return team_key
    return f"{team.team_name} ({team.team_key})"


def _manual_template_payload(soccer_entities: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "league": "SOCCER",
        "source": MANUAL_TEMPLATE_SOURCE,
        "source_note": (
            "Fill games from a verified schedule source before ingesting. "
            "This template intentionally contains no fabricated game rows."
        ),
        "generated_at": utc_now().isoformat(),
        "candidate_entities": soccer_entities[:100],
        "teams": [],
        "games": [],
        "team_stats": [],
        "injuries": [],
        "odds": [],
        "expected_game_schema": {
            "game_key": "SOCCER:verified:<competition>:<event-id-or-home-away-date>",
            "scheduled_at": "2026-07-08T19:00:00+00:00",
            "home_team_key": "Brazil",
            "away_team_key": "Morocco",
            "status": "scheduled",
            "source": "verified_manual",
            "source_url": "https://...",
        },
    }


def _league_breakdown(
    partial_links: list[SportsMarketLink],
    verified_links: list[SportsMarketLink],
    games: list[SportsGame],
) -> list[dict[str, Any]]:
    leagues = sorted(
        {link.league for link in partial_links + verified_links}
        | {game.league for game in games}
    )
    rows: list[dict[str, Any]] = []
    for league in leagues:
        rows.append(
            {
                "league": league,
                "partial_links": sum(1 for link in partial_links if link.league == league),
                "verified_links": sum(1 for link in verified_links if link.league == league),
                "verified_games": sum(
                    1 for game in games if game.league == league and _game_is_verified(game)
                ),
            }
        )
    return rows


def _link_provenance(link: SportsMarketLink) -> str:
    raw = decode_json(link.raw_json)
    source = str(raw.get("source") or "").lower()
    reason = str(link.link_reason or "").lower()
    game_key = str(link.game_key or "").lower()
    if source == VERIFIED_SOURCE:
        return VERIFIED_SOURCE
    if (
        "market-derived" in game_key
        or source == "market-derived-fallback"
        or "market-derived" in reason
    ):
        return PARTIAL_SOURCE
    return source or "unknown"


def _game_is_verified(game: SportsGame) -> bool:
    raw = decode_json(game.raw_json)
    source = str(raw.get("source") or "").lower()
    game_key = str(game.game_key or "").lower()
    status = str(game.status or "").lower()
    return not (
        source == "kalshi_event_derived"
        or "kalshi-event-derived" in game_key
        or "market-derived" in game_key
        or status == "kalshi_event_derived"
    )


def _next_action(*, soccer_partials: int, soccer_games: int, suspect_verified: int) -> str:
    if suspect_verified:
        return (
            "Review suspect verified sports links before trusting them for model rewards; "
            "rerun Phase 3AE after the new ambiguity guards."
        )
    if soccer_partials and not soccer_games:
        return (
            "Use the manual soccer template or a verified international competition feed, "
            "then rerun Phase 3AE."
        )
    return "Rerun Phase 3AE and market coverage after refreshing verified soccer schedules."


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AG Sports Ambiguity + Soccer Coverage",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Max schedule delta hours: {payload['max_schedule_delta_hours']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## League Breakdown",
            "",
            "| League | Partial links | Verified links | Verified games |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for row in payload["league_breakdown"]:
        lines.append(
            f"| {row['league']} | {row['partial_links']} | "
            f"{row['verified_links']} | {row['verified_games']} |"
        )
    lines.extend(
        [
            "",
            "## Soccer Coverage",
            "",
            f"- Status: {payload['soccer_coverage']['status']}",
            f"- Manual template: `{payload['soccer_coverage']['manual_template_path']}`",
            "- Recommended ESPN competitions:",
            "",
            ", ".join(payload["soccer_coverage"]["recommended_competitions"]),
            "",
            "### Top Soccer Entities",
            "",
            "| Entity | Count | Example ticker |",
            "| --- | ---: | --- |",
        ]
    )
    for row in payload["soccer_coverage"]["top_entities"][:20]:
        lines.append(f"| {_md(row['entity'])} | {row['count']} | `{row['example_ticker']}` |")
    if not payload["soccer_coverage"]["top_entities"]:
        lines.append("| none | 0 |  |")
    lines.extend(
        [
            "",
            "## Ambiguity Diagnostics",
            "",
            "### Guardrails",
            "",
        ]
    )
    for guardrail in payload["ambiguity_diagnostics"]["guardrails"]:
        lines.append(f"- {guardrail}")
    lines.extend(
        [
            "",
            "### Suspect Verified Links",
            "",
            "| Ticker | Game | Reasons | Conflicting teams |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in payload["ambiguity_diagnostics"]["suspect_verified_links"][:25]:
        lines.append(
            f"| `{row['ticker']}` | `{row['game_key']}` | "
            f"{', '.join(row['reasons'])} | {_md(', '.join(row['conflicting_teams']))} |"
        )
    if not payload["ambiguity_diagnostics"]["suspect_verified_links"]:
        lines.append("| none |  |  |  |")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Safety",
            "",
            "- Paper-only diagnostics and schedule templates.",
            "- No demo orders.",
            "- No live orders.",
            "- No automatic deletion of existing links.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_repair_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AG Sports Link Repair Pass",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Phase 3AE source: `{payload['phase3ae_source_path']}`",
        f"- Max schedule delta hours: {payload['max_schedule_delta_hours']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Auto-Upgrade Policy",
            "",
            f"- Auto upgrades created: {payload['auto_upgrade_policy']['auto_upgrades_created']}",
            f"- Policy: {payload['auto_upgrade_policy']['policy']}",
            "",
            "## Cause Breakdown",
            "",
            "| Cause | Count |",
            "| --- | ---: |",
        ]
    )
    for row in payload["cause_breakdown"]:
        lines.append(f"| {row['cause']} | {row['count']} |")
    if not payload["cause_breakdown"]:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Missing Alias Candidates",
            "",
            "| League | Entity | Role | Count | Examples |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    for row in payload["missing_alias_candidates"][:40]:
        examples = ", ".join(f"`{ticker}`" for ticker in row["example_tickers"])
        lines.append(
            f"| {row['league']} | {_md(row['entity'])} | {row['entity_role']} | "
            f"{row['count']} | {examples} |"
        )
    if not payload["missing_alias_candidates"]:
        lines.append("| none |  |  | 0 |  |")
    lines.extend(
        [
            "",
            "## Manual Disambiguation Candidates",
            "",
            "| Ticker | Cause | League | Type | Clean candidates | Unmatched entities |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for row in payload["manual_disambiguation_candidates"][:40]:
        lines.append(
            f"| `{row['ticker']}` | {row['primary_cause']} | {row['league']} | "
            f"{row['market_type']} | {row['clean_candidate_count']} | "
            f"{_md(', '.join(row['unmatched_entities']))} |"
        )
    if not payload["manual_disambiguation_candidates"]:
        lines.append("| none |  |  |  | 0 |  |")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Safety",
            "",
            "- Report-only repair pass.",
            "- No demo orders.",
            "- No live orders.",
            "- No sports links are created or upgraded by this command.",
            "- Clean manual candidates must still go through Phase 3AE before any link write.",
            "",
        ]
    )
    return "\n".join(lines)


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
