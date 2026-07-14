from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    Market,
    MarketLeg,
    SportsGame,
    SportsMarketLink,
    SportsTeam,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ae import (
    DEFAULT_MAX_SCHEDULE_DELTA_HOURS,
    DEFAULT_ROSTER_EVIDENCE_PATH,
    PHASE_3AE_VERSION,
    UNKNOWN,
    VerifiedRosterEvidence,
    _candidate_games,
    _classification_with_link_defaults,
    _evidence_valid_for_game,
    _games_by_league,
    _has_clean_schedule_time,
    _has_round_placeholder_team,
    _has_team_conflict,
    _load_roster_evidence,
    _markets_by_ticker,
    _mentioned_team_keys,
    _normalized_key,
    _partial_links_without_verified_link,
    _phrase_in_text,
    _roster_evidence_by_league,
    _roster_evidence_payload,
    _schedule_window_allowed,
    _team_alias_index,
    _verified_games,
)
from kalshi_predictor.sports.classifier import classify_sports_market
from kalshi_predictor.sports.repository import sports_teams
from kalshi_predictor.utils.time import utc_now

PHASE_3AE_ROSTER_CANDIDATE_VERSION = "phase3ae_roster_candidate_diagnostics_v1"
DEFAULT_REWORK_QUEUE_PATH = Path("reports/phase3ah_sports/phase3ah_roster_rework_queue.json")

MARKET_NOT_FOUND = "MARKET_NOT_FOUND"
NOT_PLAYER_PROP = "NOT_PLAYER_PROP"
NO_VERIFIED_ROSTER_PLAYER_MENTIONED = "NO_VERIFIED_ROSTER_PLAYER_MENTIONED"
PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW = "PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW"
NO_VERIFIED_GAMES_IN_TIME_WINDOW = "NO_VERIFIED_GAMES_IN_TIME_WINDOW"
MULTIPLE_CLEAN_GAME_CANDIDATES = "MULTIPLE_CLEAN_GAME_CANDIDATES"
MARKET_TYPE_NOT_CLEAN = "MARKET_TYPE_NOT_CLEAN"
MISSING_MARKET_CLOSE_TIME = "MISSING_MARKET_CLOSE_TIME"
MISSING_GAME_TIME = "MISSING_GAME_TIME"
TEAM_CONFLICT = "TEAM_CONFLICT"
CLEAN_PHASE3AE_CANDIDATE = "CLEAN_PHASE3AE_CANDIDATE"
MIXED_SPORT_PLAYER_LEGS = "MIXED_SPORT_PLAYER_LEGS"
ROUND_PLACEHOLDER_GAME = "ROUND_PLACEHOLDER_GAME"

CROSS_SPORT_PLAYER_LEAGUE_HINTS = {
    "a ja wilson": "WNBA",
    "aliyah boston": "WNBA",
    "angel reese": "WNBA",
    "aja wilson": "WNBA",
    "braxton ashcraft": "MLB",
    "caitlin clark": "WNBA",
    "courtney williams": "WNBA",
    "eury perez": "MLB",
    "erick fedde": "MLB",
    "gabby williams": "WNBA",
    "gage jump": "MLB",
    "griffin jax": "MLB",
    "jacob degrom": "MLB",
    "kahleah copper": "WNBA",
    "kelsey mitchell": "WNBA",
    "kodai senga": "MLB",
    "monique billings": "WNBA",
    "olivia miles": "WNBA",
    "pete crow armstrong": "MLB",
    "sabrina ionescu": "WNBA",
    "shohei ohtani": "MLB",
    "sonia citron": "WNBA",
    "tanner bibee": "MLB",
    "tarik skubal": "MLB",
    "vladimir guerrero jr": "MLB",
    "vladimir guerrero jr.": "MLB",
}

NON_PLAYER_ROSTER_ENTITIES = {
    "boston",
    "congo dr",
    "congo dr wins by more than goals",
    "democratic republic of the congo",
    "dr congo",
    "los angeles d",
    "san francisco",
}


@dataclass(frozen=True)
class Phase3AERosterCandidateArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    clean_candidates_path: Path
    blockers_path: Path
    manual_disambiguation_path: Path


def build_phase3ae_roster_candidate_diagnostics(
    session: Session,
    *,
    output_dir: Path | None = None,
    roster_evidence_path: Path | None = DEFAULT_ROSTER_EVIDENCE_PATH,
    rework_queue_path: Path | None = DEFAULT_REWORK_QUEUE_PATH,
    limit: int | None = None,
    max_schedule_delta_hours: int | None = DEFAULT_MAX_SCHEDULE_DELTA_HOURS,
) -> dict[str, Any]:
    """Diagnose player-prop roster evidence against Phase 3AE's strict gates.

    This is a read-only report. It does not create sports links, features, orders, or
    execution state.
    """
    del output_dir
    session.flush()
    partial_links = _partial_links_without_verified_link(session, limit=limit)
    markets_by_ticker = _markets_by_ticker(session, [link.ticker for link in partial_links])
    verified_games = _verified_games(session)
    verified_games_by_league = _games_by_league(verified_games)
    teams = sports_teams(session, league="ALL")
    team_by_key = {team.team_key: team for team in teams}
    roster_evidence = _load_roster_evidence(roster_evidence_path)
    roster_evidence_by_league = _roster_evidence_by_league(roster_evidence)
    market_legs_by_ticker = _market_legs_by_ticker(
        session,
        [link.ticker for link in partial_links],
    )
    rework_rows = _load_json_list(rework_queue_path)

    rows = [
        _diagnose_link(
            link,
            market=markets_by_ticker.get(link.ticker),
            verified_games=verified_games,
            verified_games_by_league=verified_games_by_league,
            team_by_key=team_by_key,
            roster_evidence=roster_evidence,
            roster_evidence_by_league=roster_evidence_by_league,
            market_legs=market_legs_by_ticker.get(link.ticker, []),
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
        for link in partial_links
    ]
    clean_candidates = [
        row for row in rows if row["upgrade_candidate_status"] == CLEAN_PHASE3AE_CANDIDATE
    ]
    blockers = [
        row for row in rows if row["upgrade_candidate_status"] != CLEAN_PHASE3AE_CANDIDATE
    ]
    blockers_by_reason = _blockers_by_reason(rows)
    blocked_tickers = {
        str(row["ticker"])
        for row in rows
        if NO_VERIFIED_ROSTER_PLAYER_MENTIONED in row["rejection_reasons"]
    }
    mixed_sport_tickers = {
        str(row["ticker"])
        for row in rows
        if MIXED_SPORT_PLAYER_LEGS in row["rejection_reasons"]
    }
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AE",
        "phase_version": PHASE_3AE_VERSION,
        "diagnostic_version": PHASE_3AE_ROSTER_CANDIDATE_VERSION,
        "mode": "PAPER_ONLY_ROSTER_CANDIDATE_DIAGNOSTICS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "roster_evidence_path": str(roster_evidence_path) if roster_evidence_path else None,
        "rework_queue_path": str(rework_queue_path) if rework_queue_path else None,
        "max_schedule_delta_hours": max_schedule_delta_hours,
        "summary": {
            "partial_links_reviewed": len(partial_links),
            "verified_games_seen": len(verified_games),
            "verified_roster_rows_seen": len(roster_evidence),
            "clean_phase3ae_candidates": len(clean_candidates),
            "blocked_candidates": len(blockers),
            "player_prop_rows": sum(1 for row in rows if row["market_type"] == "PLAYER_PROP"),
            "manual_disambiguation_rows": sum(
                1
                for row in rows
                if row["upgrade_candidate_status"] == MULTIPLE_CLEAN_GAME_CANDIDATES
            ),
            "mixed_sport_player_leg_rows": sum(
                1 for row in rows if row.get("cross_sport_player_entities")
            ),
            "round_placeholder_game_rows": sum(
                1 for row in rows if ROUND_PLACEHOLDER_GAME in row.get("rejection_reasons", [])
            ),
        },
        "blockers_by_reason": blockers_by_reason,
        "top_missing_roster_players": _top_missing_roster_players(
            rows,
            market_legs_by_ticker=market_legs_by_ticker,
            rework_rows=rework_rows,
            roster_evidence=roster_evidence,
            mixed_sport_tickers=mixed_sport_tickers,
        ),
        "top_cross_sport_player_leaks": _top_cross_sport_player_leaks(rows),
        "top_round_placeholder_games": _top_round_placeholder_games(rows),
        "top_schedule_windows_to_backfill": _top_schedule_windows(rows),
        "top_ambiguous_games": _top_ambiguous_games(rows),
        "next_20_rows_to_verify": _next_rows_to_verify(
            rework_rows,
            blocked_tickers=blocked_tickers,
            mixed_sport_tickers=mixed_sport_tickers,
            roster_evidence=roster_evidence,
        ),
        "clean_candidates": clean_candidates,
        "manual_disambiguation_candidates": _manual_disambiguation_rows(rows),
        "rows": rows,
        "recommended_next_action": _recommended_next_action(
            clean_count=len(clean_candidates),
            blockers_by_reason=blockers_by_reason,
        ),
    }


def write_phase3ae_roster_candidate_diagnostics(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ae_roster_candidates"),
    roster_evidence_path: Path | None = DEFAULT_ROSTER_EVIDENCE_PATH,
    rework_queue_path: Path | None = DEFAULT_REWORK_QUEUE_PATH,
    limit: int | None = None,
    max_schedule_delta_hours: int | None = DEFAULT_MAX_SCHEDULE_DELTA_HOURS,
) -> Phase3AERosterCandidateArtifactSet:
    payload = build_phase3ae_roster_candidate_diagnostics(
        session,
        output_dir=output_dir,
        roster_evidence_path=roster_evidence_path,
        rework_queue_path=rework_queue_path,
        limit=limit,
        max_schedule_delta_hours=max_schedule_delta_hours,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ae_roster_candidate_diagnostics.json"
    markdown_path = output_dir / "phase3ae_roster_candidate_diagnostics.md"
    clean_candidates_path = output_dir / "phase3ae_clean_roster_link_candidates.json"
    blockers_path = output_dir / "phase3ae_roster_candidate_blockers.json"
    manual_disambiguation_path = output_dir / "phase3ae_manual_disambiguation_candidates.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    clean_candidates_path.write_text(
        json.dumps(payload["clean_candidates"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    blockers_path.write_text(
        json.dumps(
            [row for row in payload["rows"] if row not in payload["clean_candidates"]],
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    manual_disambiguation_path.write_text(
        json.dumps(
            payload["manual_disambiguation_candidates"],
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    return Phase3AERosterCandidateArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        clean_candidates_path=clean_candidates_path,
        blockers_path=blockers_path,
        manual_disambiguation_path=manual_disambiguation_path,
    )


def _diagnose_link(
    link: SportsMarketLink,
    *,
    market: Market | None,
    verified_games: list[SportsGame],
    verified_games_by_league: dict[str, list[SportsGame]],
    team_by_key: dict[str, SportsTeam],
    roster_evidence: list[VerifiedRosterEvidence],
    roster_evidence_by_league: dict[str, list[VerifiedRosterEvidence]],
    market_legs: list[MarketLeg],
    max_schedule_delta_hours: int | None,
) -> dict[str, Any]:
    if market is None:
        return _base_row(
            link,
            market=None,
            market_type=str(link.market_type or UNKNOWN).upper(),
            status=MARKET_NOT_FOUND,
            rejection_reasons=[MARKET_NOT_FOUND],
            next_action="Restore or ingest the missing market row before roster diagnostics.",
        )

    raw_classification = classify_sports_market(market)
    classification = _classification_with_link_defaults(raw_classification, link)
    market_type = str(classification.get("market_type") or link.market_type or UNKNOWN).upper()
    league = str(classification.get("league") or link.league or UNKNOWN).upper()
    if market_type != "PLAYER_PROP":
        row = _base_row(
            link,
            market=market,
            market_type=market_type,
            status=NOT_PLAYER_PROP,
            rejection_reasons=[NOT_PLAYER_PROP],
            next_action=_next_action(NOT_PLAYER_PROP),
        )
        row.update(
            {
                "league": league if league != UNKNOWN else link.league,
                "raw_classifier_market_type": raw_classification.get("market_type"),
                "mentioned_team_keys": [],
                "market_leg_entities": _market_leg_entities(market_legs),
                "classification": {
                    "league": classification.get("league"),
                    "market_type": classification.get("market_type"),
                    "entities": classification.get("entities", []),
                },
            }
        )
        return row

    candidate_games = _candidate_games(
        link,
        verified_games,
        games_by_league=verified_games_by_league,
    )
    window_games = [
        game
        for game in candidate_games
        if _schedule_window_allowed(
            market,
            game,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
    ]
    evidence_pool = _roster_pool(league, roster_evidence, roster_evidence_by_league)
    text = _classification_text(classification, market)
    player_prop_entities = _player_prop_leg_entities(market_legs)
    entity_leagues = _player_entity_leagues(
        player_prop_entities,
        target_league=league,
        roster_evidence=roster_evidence,
    )
    target_player_prop_entities = [
        entity
        for entity in player_prop_entities
        if not _is_cross_sport_player_league(entity_leagues.get(entity), target_league=league)
    ]
    non_player_roster_entities = [
        entity for entity in target_player_prop_entities if _is_non_player_roster_entity(entity)
    ]
    target_player_prop_entities = [
        entity
        for entity in target_player_prop_entities
        if not _is_non_player_roster_entity(entity)
    ]
    cross_sport_entities = [
        {
            "entity_name": entity,
            "inferred_league": entity_leagues[entity],
            "target_league": league,
        }
        for entity in player_prop_entities
        if _is_cross_sport_player_league(entity_leagues.get(entity), target_league=league)
    ]
    matched_evidence = [
        evidence
        for evidence in evidence_pool
        if (
            _phrase_in_text(text, evidence.player_name)
            or any(
                _phrase_in_text(entity, evidence.player_name)
                for entity in target_player_prop_entities
            )
        )
    ]
    missing_roster_entities = [
        entity
        for entity in target_player_prop_entities
        if not any(_phrase_in_text(entity, evidence.player_name) for evidence in matched_evidence)
    ]
    unsupported_component_legs = _unsupported_player_prop_component_legs(market_legs)
    mentioned_team_keys = (
        _mentioned_team_keys(market, _candidate_team_aliases(window_games, team_by_key))
        if matched_evidence
        else set()
    )
    clean_games: list[dict[str, Any]] = []
    candidate_summaries: list[dict[str, Any]] = []
    rejection_reasons: list[str] = []

    raw_market_type = str(raw_classification.get("market_type") or UNKNOWN).upper()
    if raw_market_type not in {UNKNOWN, "PLAYER_PROP"} and market_type == "PLAYER_PROP":
        rejection_reasons.append(MARKET_TYPE_NOT_CLEAN)
    if unsupported_component_legs and market_type == "PLAYER_PROP":
        rejection_reasons.append(MARKET_TYPE_NOT_CLEAN)
    if cross_sport_entities and market_type == "PLAYER_PROP":
        rejection_reasons.append(MIXED_SPORT_PLAYER_LEGS)
    if (
        target_player_prop_entities
        and (not matched_evidence or missing_roster_entities)
        and market_type == "PLAYER_PROP"
        and not cross_sport_entities
    ):
        rejection_reasons.append(NO_VERIFIED_ROSTER_PLAYER_MENTIONED)
    if market.close_time is None and _requires_clean_time(max_schedule_delta_hours):
        rejection_reasons.append(MISSING_MARKET_CLOSE_TIME)
    if not window_games and candidate_games:
        rejection_reasons.append(NO_VERIFIED_GAMES_IN_TIME_WINDOW)
    if not candidate_games:
        rejection_reasons.append(NO_VERIFIED_GAMES_IN_TIME_WINDOW)

    for game in window_games:
        clean_result = _clean_game_candidate(
            market,
            game=game,
            matched_evidence=matched_evidence,
            mentioned_team_keys=mentioned_team_keys,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
        candidate_summaries.append(
            _game_summary(
                game,
                matched_players=[
                    evidence.player_name for evidence in clean_result["team_matched_evidence"]
                ],
                rejection_reasons=clean_result["rejection_reasons"],
            )
        )
        rejection_reasons.extend(clean_result["rejection_reasons"])
        if clean_result["clean"] and not missing_roster_entities and not cross_sport_entities:
            clean_games.append(
                _game_summary(
                    game,
                    matched_players=[
                        evidence.player_name for evidence in clean_result["team_matched_evidence"]
                    ],
                    rejection_reasons=[],
                )
            )

    rejection_reasons = _dedupe(rejection_reasons)
    if (
        len(clean_games) == 1
        and market_type == "PLAYER_PROP"
        and MARKET_TYPE_NOT_CLEAN not in rejection_reasons
    ):
        status = CLEAN_PHASE3AE_CANDIDATE
        rejection_reasons = [CLEAN_PHASE3AE_CANDIDATE]
    elif len(clean_games) > 1 and market_type == "PLAYER_PROP":
        status = MULTIPLE_CLEAN_GAME_CANDIDATES
        rejection_reasons = _with_primary(rejection_reasons, MULTIPLE_CLEAN_GAME_CANDIDATES)
    else:
        status = _primary_status(rejection_reasons, matched_evidence)
        if (
            market_type == "PLAYER_PROP"
            and matched_evidence
            and not missing_roster_entities
            and not cross_sport_entities
            and PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW not in rejection_reasons
            and not clean_games
        ):
            rejection_reasons = _with_primary(
                rejection_reasons,
                PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW,
            )
            status = PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW

    row = _base_row(
        link,
        market=market,
        market_type=market_type,
        status=status,
        rejection_reasons=rejection_reasons,
        next_action=_next_action(status),
    )
    row.update(
        {
            "league": league if league != UNKNOWN else link.league,
            "raw_classifier_market_type": raw_market_type,
            "matched_verified_players": [
                _roster_evidence_payload(evidence) for evidence in matched_evidence
            ],
            "matched_roster_team_keys": sorted(
                {_normalized_key(evidence.current_team_key) for evidence in matched_evidence}
            ),
            "missing_roster_entities": missing_roster_entities,
            "suppressed_roster_entities": [
                {
                    "entity_name": entity,
                    "reason": "TEAM_OR_COMPETITION_ENTITY",
                    "verified_entity_type": "TEAM_OR_COMPETITION_ENTITY",
                }
                for entity in non_player_roster_entities
            ],
            "cross_sport_player_entities": cross_sport_entities,
            "mentioned_team_keys": sorted(mentioned_team_keys),
            "candidate_schedule_games": candidate_summaries[:25],
            "candidate_schedule_game_count": len(window_games),
            "clean_candidate_games": clean_games,
            "clean_candidate_count": len(clean_games),
            "unsupported_component_legs": unsupported_component_legs,
            "market_leg_entities": _market_leg_entities(market_legs),
            "classification": {
                "league": classification.get("league"),
                "market_type": classification.get("market_type"),
                "entities": classification.get("entities", []),
            },
        }
    )
    return row


def _clean_game_candidate(
    market: Market,
    *,
    game: SportsGame,
    matched_evidence: list[VerifiedRosterEvidence],
    mentioned_team_keys: set[str],
    max_schedule_delta_hours: int | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if _has_round_placeholder_team(game):
        reasons.append(ROUND_PLACEHOLDER_GAME)
    if game.scheduled_at is None and _requires_clean_time(max_schedule_delta_hours):
        reasons.append(MISSING_GAME_TIME)
    if not _has_clean_schedule_time(
        market,
        game,
        max_schedule_delta_hours=max_schedule_delta_hours,
    ):
        if market.close_time is None and _requires_clean_time(max_schedule_delta_hours):
            reasons.append(MISSING_MARKET_CLOSE_TIME)
        if game.scheduled_at is None and _requires_clean_time(max_schedule_delta_hours):
            reasons.append(MISSING_GAME_TIME)
        reasons.append(NO_VERIFIED_GAMES_IN_TIME_WINDOW)
    if _has_team_conflict(game, mentioned_team_keys=mentioned_team_keys):
        reasons.append(TEAM_CONFLICT)
    game_team_keys = {_normalized_key(game.home_team_key), _normalized_key(game.away_team_key)}
    valid_evidence = [
        evidence
        for evidence in matched_evidence
        if _evidence_valid_for_game(evidence, market=market, game=game)
    ]
    evidence_team_keys = {_normalized_key(evidence.current_team_key) for evidence in valid_evidence}
    team_matched_evidence = [
        evidence
        for evidence in valid_evidence
        if _normalized_key(evidence.current_team_key) in game_team_keys
    ]
    if not valid_evidence or not evidence_team_keys <= game_team_keys:
        reasons.append(PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW)
    normalized_mentions = {_normalized_key(team_key) for team_key in mentioned_team_keys}
    if normalized_mentions and not normalized_mentions <= game_team_keys:
        reasons.append(TEAM_CONFLICT)
    reasons = _dedupe(reasons)
    return {
        "clean": not reasons,
        "rejection_reasons": reasons,
        "team_matched_evidence": team_matched_evidence,
    }


def _base_row(
    link: SportsMarketLink,
    *,
    market: Market | None,
    market_type: str,
    status: str,
    rejection_reasons: list[str],
    next_action: str,
) -> dict[str, Any]:
    return {
        "ticker": link.ticker,
        "market_title": market.title if market is not None else "",
        "league": link.league,
        "market_type": market_type,
        "market_close_time": market.close_time.isoformat() if market and market.close_time else "",
        "partial_game_key": link.game_key,
        "matched_verified_players": [],
        "matched_roster_team_keys": [],
        "missing_roster_entities": [],
        "cross_sport_player_entities": [],
        "candidate_schedule_games": [],
        "candidate_schedule_game_count": 0,
        "clean_candidate_games": [],
        "clean_candidate_count": 0,
        "rejection_reasons": _dedupe(rejection_reasons),
        "upgrade_candidate_status": status,
        "next_action": next_action,
    }


def _roster_pool(
    league: str,
    roster_evidence: list[VerifiedRosterEvidence],
    by_league: dict[str, list[VerifiedRosterEvidence]],
) -> list[VerifiedRosterEvidence]:
    if league in {"", "ALL", "SPORTS", UNKNOWN}:
        return roster_evidence
    return by_league.get(league, [])


def _classification_text(classification: dict[str, Any], market: Market) -> str:
    text = str(classification.get("text") or "").strip()
    if text:
        return text
    return " ".join(
        str(value or "")
        for value in (
            market.ticker,
            market.title,
            market.subtitle,
            market.event_ticker,
            market.series_ticker,
            market.rules_primary,
            market.rules_secondary,
        )
    )


def _game_summary(
    game: SportsGame,
    *,
    matched_players: list[str],
    rejection_reasons: list[str],
) -> dict[str, Any]:
    return {
        "league": game.league,
        "game_key": game.game_key,
        "scheduled_at": game.scheduled_at.isoformat() if game.scheduled_at else "",
        "home_team_key": game.home_team_key,
        "away_team_key": game.away_team_key,
        "matched_players": sorted(set(matched_players)),
        "rejection_reasons": _dedupe(rejection_reasons),
    }


def _candidate_team_aliases(
    games: list[SportsGame],
    team_by_key: dict[str, SportsTeam],
) -> list[tuple[str, list[str]]]:
    team_keys = sorted(
        {
            team_key
            for game in games
            for team_key in (game.home_team_key, game.away_team_key)
            if team_key
        }
    )
    return _team_alias_index([team_by_key[key] for key in team_keys if key in team_by_key])


def _requires_clean_time(max_schedule_delta_hours: int | None) -> bool:
    return max_schedule_delta_hours is not None and max_schedule_delta_hours > 0


def _primary_status(
    rejection_reasons: list[str],
    matched_evidence: list[VerifiedRosterEvidence],
) -> str:
    priority = [
        MARKET_NOT_FOUND,
        NOT_PLAYER_PROP,
        MARKET_TYPE_NOT_CLEAN,
        MIXED_SPORT_PLAYER_LEGS,
        NO_VERIFIED_ROSTER_PLAYER_MENTIONED,
        ROUND_PLACEHOLDER_GAME,
        MISSING_MARKET_CLOSE_TIME,
        MISSING_GAME_TIME,
        NO_VERIFIED_GAMES_IN_TIME_WINDOW,
        TEAM_CONFLICT,
        PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW,
    ]
    for reason in priority:
        if reason in rejection_reasons:
            return reason
    if matched_evidence:
        return PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW
    return NO_VERIFIED_ROSTER_PLAYER_MENTIONED


def _next_action(status: str) -> str:
    if status == CLEAN_PHASE3AE_CANDIDATE:
        return "Rerun Phase 3AE; this row clears roster, team, time, and market-type gates."
    if status == MULTIPLE_CLEAN_GAME_CANDIDATES:
        return "Add manual disambiguation or tighten schedule evidence before any upgrade."
    if status == MIXED_SPORT_PLAYER_LEGS:
        return (
            "Fix cross-sport leg extraction/classification; do not fill target-league "
            "roster evidence."
        )
    if status == NO_VERIFIED_ROSTER_PLAYER_MENTIONED:
        return "Fill Phase 3AH roster evidence for the player entity in this market."
    if status == ROUND_PLACEHOLDER_GAME:
        return "Replace round placeholder teams with verified teams before any link upgrade."
    if status == PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW:
        return "Backfill schedule windows or verify the player's roster team for the game date."
    if status == NOT_PLAYER_PROP:
        return "Leave this row to team/game schedule matching; roster evidence is not applicable."
    if status == TEAM_CONFLICT:
        return "Resolve team aliases or manual game choice before the row can be upgraded."
    if status in {MISSING_MARKET_CLOSE_TIME, MISSING_GAME_TIME, NO_VERIFIED_GAMES_IN_TIME_WINDOW}:
        return "Backfill verified schedules around the market close window."
    return "Review this row manually before any link upgrade."


def _with_primary(reasons: list[str], primary: str) -> list[str]:
    return [primary, *[reason for reason in _dedupe(reasons) if reason != primary]]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _blockers_by_reason(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for row in rows:
        if row.get("upgrade_candidate_status") == CLEAN_PHASE3AE_CANDIDATE:
            continue
        for reason in row.get("rejection_reasons", []):
            counter[str(reason)] += 1
            examples.setdefault(str(reason), [])
            if len(examples[str(reason)]) < 5:
                examples[str(reason)].append(str(row.get("ticker") or ""))
    return [
        {"reason": reason, "count": count, "example_tickers": examples.get(reason, [])}
        for reason, count in counter.most_common()
    ]


def _top_missing_roster_players(
    rows: list[dict[str, Any]],
    *,
    market_legs_by_ticker: dict[str, list[MarketLeg]],
    rework_rows: list[dict[str, Any]],
    roster_evidence: list[VerifiedRosterEvidence],
    mixed_sport_tickers: set[str],
) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str]] = Counter()
    examples: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        if NO_VERIFIED_ROSTER_PLAYER_MENTIONED not in row.get("rejection_reasons", []):
            continue
        ticker = str(row.get("ticker") or "")
        missing_entities = [str(entity) for entity in row.get("missing_roster_entities", [])]
        if not missing_entities:
            missing_entities = [
                str(leg.entity_name or "").strip()
                for leg in market_legs_by_ticker.get(ticker, [])
                if str(leg.market_type or "").upper() == "PLAYER_PROP"
                and str(leg.entity_name or "").strip()
            ]
        for entity in missing_entities:
            if _is_non_player_roster_entity(entity):
                continue
            if _has_target_roster_evidence(
                entity,
                target_league=str(row.get("league") or UNKNOWN),
                roster_evidence=roster_evidence,
            ):
                continue
            if _is_cross_sport_player_name(
                entity,
                target_league=str(row.get("league") or UNKNOWN),
                roster_evidence=roster_evidence,
            ):
                continue
            key = (str(row.get("league") or "UNKNOWN"), entity)
            counter[key] += 1
            examples.setdefault(key, [])
            if len(examples[key]) < 5:
                examples[key].append(ticker)
    if not counter:
        for row in rework_rows:
            entity = str(row.get("player_name") or "").strip()
            if not entity:
                continue
            if _is_non_player_roster_entity(entity):
                continue
            if _has_target_roster_evidence(
                entity,
                target_league=str(row.get("league") or UNKNOWN),
                roster_evidence=roster_evidence,
            ):
                continue
            rework_tickers = {
                str(ticker)
                for ticker in row.get("example_player_prop_tickers", [])
                if str(ticker or "").strip()
            }
            if rework_tickers and rework_tickers <= mixed_sport_tickers:
                continue
            if _is_cross_sport_player_name(
                entity,
                target_league=str(row.get("league") or UNKNOWN),
                roster_evidence=roster_evidence,
            ):
                continue
            key = (str(row.get("league") or "UNKNOWN"), entity)
            counter[key] += int(row.get("count") or 0)
            examples[key] = [
                str(ticker) for ticker in row.get("example_player_prop_tickers", [])[:5]
            ]
    return [
        {
            "league": league,
            "player_name": player_name,
            "count": count,
            "example_tickers": examples.get((league, player_name), []),
            "verified_entity_type": "PLAYER",
            "blocks_roster_evidence": False,
            "next_action": "Verify roster ID, current team, source URL, and valid_from.",
        }
        for (league, player_name), count in counter.most_common(20)
    ]


def _top_cross_sport_player_leaks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str, str]] = Counter()
    examples: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "")
        for entity in row.get("cross_sport_player_entities", []):
            key = (
                str(entity.get("target_league") or row.get("league") or "UNKNOWN"),
                str(entity.get("inferred_league") or UNKNOWN),
                str(entity.get("entity_name") or "").strip(),
            )
            if not key[2]:
                continue
            counter[key] += 1
            examples.setdefault(key, [])
            if len(examples[key]) < 5:
                examples[key].append(ticker)
    return [
        {
            "target_league": target_league,
            "inferred_league": inferred_league,
            "player_name": player_name,
            "count": count,
            "example_tickers": examples.get((target_league, inferred_league, player_name), []),
            "next_action": "Repair leg classification; keep this out of target-league roster gaps.",
        }
        for (target_league, inferred_league, player_name), count in counter.most_common(20)
    ]


def _top_round_placeholder_games(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str, str, str]] = Counter()
    examples: dict[tuple[str, str, str, str], list[str]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "")
        for game in row.get("candidate_schedule_games", []):
            if ROUND_PLACEHOLDER_GAME not in game.get("rejection_reasons", []):
                continue
            key = (
                str(game.get("league") or row.get("league") or "UNKNOWN"),
                str(game.get("game_key") or ""),
                str(game.get("home_team_key") or ""),
                str(game.get("away_team_key") or ""),
            )
            counter[key] += 1
            examples.setdefault(key, [])
            if len(examples[key]) < 5:
                examples[key].append(ticker)
    return [
        {
            "league": league,
            "game_key": game_key,
            "home_team_key": home_team_key,
            "away_team_key": away_team_key,
            "count": count,
            "example_tickers": examples.get((league, game_key, home_team_key, away_team_key), []),
            "next_action": "Replace placeholder round teams with official verified teams.",
        }
        for (league, game_key, home_team_key, away_team_key), count in counter.most_common(20)
    ]


def _top_schedule_windows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str, str]] = Counter()
    examples: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        reasons = set(row.get("rejection_reasons", []))
        if not row.get("matched_verified_players"):
            continue
        if reasons.isdisjoint(
            {
                PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW,
                NO_VERIFIED_GAMES_IN_TIME_WINDOW,
                MISSING_GAME_TIME,
                MISSING_MARKET_CLOSE_TIME,
            }
        ):
            continue
        close_date = str(row.get("market_close_time") or "")[:10] or "UNKNOWN_DATE"
        team_keys = ",".join(row.get("matched_roster_team_keys", [])) or "UNKNOWN_TEAM"
        key = (str(row.get("league") or "UNKNOWN"), close_date, team_keys)
        counter[key] += 1
        examples.setdefault(key, [])
        if len(examples[key]) < 5:
            examples[key].append(str(row.get("ticker") or ""))
    return [
        {
            "league": league,
            "market_close_date": close_date,
            "roster_team_keys": team_keys.split(",") if team_keys != "UNKNOWN_TEAM" else [],
            "count": count,
            "example_tickers": examples.get((league, close_date, team_keys), []),
            "next_action": "Backfill verified schedules around this team/date window.",
        }
        for (league, close_date, team_keys), count in counter.most_common(20)
    ]


def _top_ambiguous_games(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ambiguous = [
        row for row in rows if row["upgrade_candidate_status"] == MULTIPLE_CLEAN_GAME_CANDIDATES
    ]
    return [
        {
            "ticker": row["ticker"],
            "league": row["league"],
            "market_title": row["market_title"],
            "clean_candidate_count": row["clean_candidate_count"],
            "clean_candidate_games": row["clean_candidate_games"],
            "next_action": "Choose one verified game manually or add stricter schedule evidence.",
        }
        for row in ambiguous[:20]
    ]


def _keep_rework_row_for_roster_queue(
    row: dict[str, Any],
    *,
    blocked_tickers: set[str],
    mixed_sport_tickers: set[str],
    roster_evidence: list[VerifiedRosterEvidence],
) -> bool:
    player_name = str(row.get("player_name") or "")
    target_league = str(row.get("league") or UNKNOWN)
    if _is_non_player_roster_entity(player_name):
        return False
    if _has_target_roster_evidence(
        player_name,
        target_league=target_league,
        roster_evidence=roster_evidence,
    ):
        return False
    if _is_cross_sport_player_name(
        player_name,
        target_league=target_league,
        roster_evidence=roster_evidence,
    ):
        return False
    tickers = {
        str(ticker)
        for ticker in row.get("example_player_prop_tickers", [])
        if str(ticker or "").strip()
    }
    if tickers and tickers.isdisjoint(blocked_tickers) and tickers & mixed_sport_tickers:
        return False
    return True


def _next_rows_to_verify(
    rework_rows: list[dict[str, Any]],
    *,
    blocked_tickers: set[str],
    mixed_sport_tickers: set[str],
    roster_evidence: list[VerifiedRosterEvidence],
) -> list[dict[str, Any]]:
    def score(row: dict[str, Any]) -> tuple[int, int, str]:
        tickers = {str(ticker) for ticker in row.get("example_player_prop_tickers", [])}
        overlap = len(tickers & blocked_tickers)
        return (overlap, int(row.get("count") or 0), str(row.get("player_name") or ""))

    ranked = [
        row
        for row in sorted(rework_rows, key=score, reverse=True)
        if _keep_rework_row_for_roster_queue(
            row,
            blocked_tickers=blocked_tickers,
            mixed_sport_tickers=mixed_sport_tickers,
            roster_evidence=roster_evidence,
        )
    ]
    if any(score(row)[0] > 0 for row in ranked):
        ranked = [row for row in ranked if score(row)[0] > 0]
    return [
        {
            "league": row.get("league"),
            "player_name": row.get("player_name"),
            "count": row.get("count", 0),
            "example_player_prop_tickers": row.get("example_player_prop_tickers", [])[:5],
            "rework_reasons": row.get("rework_reasons", []),
            "next_action": row.get("next_action")
            or "Verify roster ID, current team, source URL, and valid_from.",
        }
        for row in ranked[:20]
    ]


def _manual_disambiguation_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manual_rows: list[dict[str, Any]] = []
    for row in rows:
        if row["upgrade_candidate_status"] not in {
            CLEAN_PHASE3AE_CANDIDATE,
            MULTIPLE_CLEAN_GAME_CANDIDATES,
        }:
            continue
        manual_rows.append(
            {
                "ticker": row["ticker"],
                "league": row["league"],
                "market_type": row["market_type"],
                "market_close_time": row["market_close_time"],
                "market_title": row["market_title"],
                "matched_verified_players": row["matched_verified_players"],
                "candidate_games": row["clean_candidate_games"],
                "upgrade_candidate_status": row["upgrade_candidate_status"],
                "review_status": "UNVERIFIED",
                "chosen_game_key": "",
                "verification_source_url": "",
                "safe_to_upgrade": False,
                "safety_note": (
                    "Do not manually upgrade. Rerun Phase 3AE only when exactly one "
                    "team + time + market-type match is verified."
                ),
            }
        )
    return manual_rows


def _market_legs_by_ticker(
    session: Session,
    tickers: list[str],
) -> dict[str, list[MarketLeg]]:
    unique = sorted({ticker for ticker in tickers if ticker})
    by_ticker: dict[str, list[MarketLeg]] = {}
    for index in range(0, len(unique), 500):
        batch = unique[index : index + 500]
        for leg in session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker.in_(batch))
            .order_by(MarketLeg.ticker, MarketLeg.leg_index)
        ):
            by_ticker.setdefault(leg.ticker, []).append(leg)
    return by_ticker


def _market_leg_entities(legs: list[MarketLeg]) -> list[dict[str, Any]]:
    return [
        {
            "entity_name": leg.entity_name,
            "market_type": leg.market_type,
            "category": leg.category,
            "raw_text": leg.raw_text,
        }
        for leg in legs
        if leg.entity_name
    ]


def _player_prop_leg_entities(legs: list[MarketLeg]) -> list[str]:
    entities: list[str] = []
    for leg in legs:
        if str(leg.market_type or "").upper() != "PLAYER_PROP":
            continue
        entity = str(leg.entity_name or "").strip()
        if entity:
            entities.append(entity)
    return sorted(set(entities))


def _unsupported_player_prop_component_legs(legs: list[MarketLeg]) -> list[dict[str, Any]]:
    unsupported: list[dict[str, Any]] = []
    for leg in legs:
        if not str(leg.entity_name or "").strip():
            continue
        market_type = str(leg.market_type or "").upper()
        category = str(leg.category or "").upper()
        if market_type == "PLAYER_PROP" and category in {"", "SPORTS"}:
            continue
        unsupported.append(
            {
                "entity_name": leg.entity_name,
                "market_type": leg.market_type,
                "category": leg.category,
                "raw_text": leg.raw_text,
            }
        )
    return unsupported


def _player_entity_leagues(
    entities: list[str],
    *,
    target_league: str,
    roster_evidence: list[VerifiedRosterEvidence],
) -> dict[str, str]:
    evidence_leagues: dict[str, set[str]] = {}
    for evidence in roster_evidence:
        player_key = _normalized_player_name(evidence.player_name)
        league = str(evidence.league or "").upper()
        if player_key and league:
            evidence_leagues.setdefault(player_key, set()).add(league)

    hint_leagues = {
        _normalized_player_name(player_name): league
        for player_name, league in CROSS_SPORT_PLAYER_LEAGUE_HINTS.items()
    }
    target = str(target_league or "").upper()
    results: dict[str, str] = {}
    for entity in entities:
        player_key = _normalized_player_name(entity)
        leagues = evidence_leagues.get(player_key, set())
        if target and target in leagues:
            results[entity] = target
        elif leagues:
            results[entity] = ",".join(sorted(leagues))
        else:
            results[entity] = hint_leagues.get(player_key, UNKNOWN)
    return results


def _is_cross_sport_player_league(
    entity_league: str | None,
    *,
    target_league: str,
) -> bool:
    league = str(entity_league or "").upper()
    target = str(target_league or "").upper()
    if league in {"", UNKNOWN} or target in {"", "ALL", "SPORTS", UNKNOWN}:
        return False
    return league != target


def _is_cross_sport_player_name(
    player_name: str,
    *,
    target_league: str,
    roster_evidence: list[VerifiedRosterEvidence],
) -> bool:
    leagues = _player_entity_leagues(
        [player_name],
        target_league=target_league,
        roster_evidence=roster_evidence,
    )
    return _is_cross_sport_player_league(
        leagues.get(player_name),
        target_league=target_league,
    )


def _has_target_roster_evidence(
    player_name: str,
    *,
    target_league: str,
    roster_evidence: list[VerifiedRosterEvidence],
) -> bool:
    player_key = _normalized_player_name(player_name)
    target = str(target_league or "").upper()
    if not player_key or target in {"", "ALL", "SPORTS", UNKNOWN}:
        return False
    return any(
        _normalized_player_name(evidence.player_name) == player_key
        and str(evidence.league or "").upper() == target
        for evidence in roster_evidence
    )


def _is_non_player_roster_entity(player_name: str) -> bool:
    return _normalized_player_name(player_name) in NON_PLAYER_ROSTER_ENTITIES


def _normalized_player_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _load_json_list(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "rework_rows", "player_prop_blockers"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _recommended_next_action(
    *,
    clean_count: int,
    blockers_by_reason: list[dict[str, Any]],
) -> str:
    if clean_count:
        return (
            "Run a capped Phase 3AE smoke with roster evidence and --no-build-features; "
            "clean candidates should upgrade unless new ambiguity appears."
        )
    top_reason = blockers_by_reason[0]["reason"] if blockers_by_reason else ""
    if top_reason == MIXED_SPORT_PLAYER_LEGS:
        return "Repair mixed/cross-sport player-leg extraction before filling more roster rows."
    if top_reason == ROUND_PLACEHOLDER_GAME:
        return "Resolve round placeholder schedule rows before rerunning Phase 3AE."
    if top_reason == NO_VERIFIED_ROSTER_PLAYER_MENTIONED:
        return "Fill the next Phase 3AH roster rows before rerunning Phase 3AE."
    if top_reason in {
        PLAYER_ROSTER_TEAM_NOT_IN_SCHEDULE_WINDOW,
        NO_VERIFIED_GAMES_IN_TIME_WINDOW,
    }:
        return "Backfill verified sports schedules around the blocked close windows."
    return "Use this report to repair roster, schedule, and ambiguity blockers before linking."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AE Roster Candidate Diagnostics",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Roster evidence: {payload['roster_evidence_path']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Blockers By Reason", ""])
    for row in payload["blockers_by_reason"][:20]:
        lines.append(
            f"- {row['reason']}: {row['count']} "
            f"(examples: {', '.join(row['example_tickers'])})"
        )
    if not payload["blockers_by_reason"]:
        lines.append("- none")
    lines.extend(["", "## Clean Candidates", ""])
    for row in payload["clean_candidates"][:20]:
        games = ", ".join(game["game_key"] for game in row["clean_candidate_games"])
        players = ", ".join(player["player_name"] for player in row["matched_verified_players"])
        lines.append(f"- {row['ticker']}: {players} -> {games}")
    if not payload["clean_candidates"]:
        lines.append("- none")
    lines.extend(["", "## Cross-Sport Player-Leg Leaks", ""])
    for row in payload["top_cross_sport_player_leaks"][:20]:
        lines.append(
            f"- {row['target_league']} contains {row['inferred_league']} / "
            f"{row['player_name']}: {row['count']} "
            f"({', '.join(row['example_tickers'])})"
        )
    if not payload["top_cross_sport_player_leaks"]:
        lines.append("- none")
    lines.extend(["", "## Round Placeholder Games", ""])
    for row in payload["top_round_placeholder_games"][:20]:
        lines.append(
            f"- {row['league']} / {row['game_key']}: {row['count']} "
            f"({row['home_team_key']} vs {row['away_team_key']}; "
            f"examples: {', '.join(row['example_tickers'])})"
        )
    if not payload["top_round_placeholder_games"]:
        lines.append("- none")
    lines.extend(["", "## Next 20 Roster Rows To Verify", ""])
    for row in payload["next_20_rows_to_verify"]:
        lines.append(
            f"- {row['league']} / {row['player_name']}: {row['count']} "
            f"({', '.join(row['example_player_prop_tickers'])})"
        )
    if not payload["next_20_rows_to_verify"]:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Safety",
            "",
            "- Read-only diagnostic report.",
            "- No sports link inserts.",
            "- No feature inserts.",
            "- No demo or live orders.",
            "",
        ]
    )
    return "\n".join(lines)
