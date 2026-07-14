from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    Market,
    MarketLeg,
    SportsFeature,
    SportsGame,
    SportsMarketLink,
    SportsTeam,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ac import build_sports_provenance_snapshot
from kalshi_predictor.sports.classifier import UNKNOWN, classify_sports_market
from kalshi_predictor.sports.features import calculate_sports_feature
from kalshi_predictor.sports.linker import score_sports_market_link
from kalshi_predictor.sports.repository import (
    insert_sports_feature,
    insert_sports_market_link,
    latest_sports_feature,
    sports_games,
    sports_team_aliases,
    sports_teams,
)
from kalshi_predictor.utils.time import utc_now

PHASE_3AE_VERSION = "phase3ae_v1"
VERIFIED_SOURCE = "verified_schedule"
ROSTER_EVIDENCE_SOURCE = "phase3ah_roster_evidence"
TEAM_ALIAS_EVIDENCE_SOURCE = "phase3ah_team_alias_review"
MANUAL_DISAMBIGUATION_SOURCE = "phase3ah_manual_disambiguation"
DEFAULT_ROSTER_EVIDENCE_PATH = Path(
    "reports/phase3ah_sports/phase3ah_verified_roster_evidence.json"
)
DEFAULT_TEAM_ALIAS_REVIEW_PATH = Path(
    "reports/phase3ah_sports/phase3ah_team_alias_review_template.json"
)
DEFAULT_MANUAL_DISAMBIGUATION_PATH = Path(
    "reports/phase3ah_sports/phase3ah_manual_disambiguation_template.json"
)
AMBIGUITY_MARGIN = Decimal("0.0500")
DEFAULT_MAX_SCHEDULE_DELTA_HOURS = 18
APPROVED_REVIEW_STATUSES = {"APPROVED", "READY", "REVIEWED_VERIFIED", "VERIFIED"}
ROUND_PLACEHOLDER_TEAM_RE = re.compile(
    r"^(?:rd|round)\d{1,2}(?:$|[-_:])|"
    r"^(?:r\d{1,2}|qf|sf|semifinal|quarterfinal|final)(?:$|[-_:])|"
    r"(?:winner|loser|runner-up|placeholder|tbd|to-be-determined)"
)


@dataclass(frozen=True)
class Phase3AEArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class VerifiedSportsMatch:
    game: SportsGame
    confidence: Decimal
    reason: str
    matched_terms: list[str]
    market_type: str
    classification: dict[str, Any]
    team_alias_evidence: list[dict[str, Any]]
    roster_evidence: list[dict[str, Any]]
    manual_disambiguation: dict[str, Any] | None


@dataclass(frozen=True)
class VerifiedRosterEvidence:
    league: str
    player_name: str
    canonical_player_id: str
    current_team_key: str
    current_team_name: str
    roster_source_url: str
    valid_from: date | None
    valid_to: date | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class VerifiedTeamAliasEvidence:
    league: str
    alias: str
    canonical_team_key: str
    canonical_team_name: str
    evidence_source_url: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ManualDisambiguationEvidence:
    ticker: str
    league: str
    chosen_game_key: str
    chosen_market_type: str
    verification_source_url: str
    raw: dict[str, Any]


def run_verified_sports_schedule_connector(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int | None = None,
    candidate_game_keys: set[str] | None = None,
    min_confidence: Decimal | None = None,
    build_features: bool = True,
    refresh_features: bool = False,
    max_schedule_delta_hours: int | None = DEFAULT_MAX_SCHEDULE_DELTA_HOURS,
    roster_evidence_path: Path | None = DEFAULT_ROSTER_EVIDENCE_PATH,
    team_alias_review_path: Path | None = DEFAULT_TEAM_ALIAS_REVIEW_PATH,
    manual_disambiguation_path: Path | None = DEFAULT_MANUAL_DISAMBIGUATION_PATH,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_every: int = 0,
) -> dict[str, Any]:
    """Upgrade partial sports market links with verified schedule/team evidence.

    This is a paper-only diagnostic/linking pass. It never creates orders or touches execution
    state.
    """
    resolved = settings or get_settings()
    threshold = min_confidence or resolved.sports_min_link_confidence
    normalized_candidate_game_keys = _normalized_candidate_game_keys(candidate_game_keys)
    total_started = time.perf_counter()
    stage_seconds: dict[str, float] = {}
    session.flush()

    stage_started = time.perf_counter()
    before = _connector_provenance_snapshot(
        session,
        use_fast_snapshot=bool(normalized_candidate_game_keys),
    )
    stage_seconds["before_snapshot"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    partial_links = _partial_links_without_verified_link(session, limit=limit)
    stage_seconds["partial_links_load"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    markets_by_ticker = _markets_by_ticker(session, [link.ticker for link in partial_links])
    stage_seconds["market_rows_load"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    market_legs_by_ticker = _market_legs_by_ticker(
        session,
        [link.ticker for link in partial_links],
    )
    stage_seconds["market_legs_load"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    verified_games = _verified_games(
        session,
        candidate_game_keys=normalized_candidate_game_keys,
    )
    verified_games_by_league = _games_by_league(verified_games)
    stage_seconds["verified_games_load"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    teams = _connector_teams(
        session,
        verified_games=verified_games,
        use_candidate_team_index=bool(normalized_candidate_game_keys),
    )
    team_by_key = {team.team_key: team for team in teams}
    stage_seconds["team_index_load"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    team_alias_evidence = _load_team_alias_evidence(team_alias_review_path)
    team_alias_evidence_by_key = _team_alias_evidence_by_key(
        team_alias_evidence,
        team_by_key=team_by_key,
    )
    team_rows = [
        _team_mapping(team, extra_aliases=team_alias_evidence_by_key.get(team.team_key, []))
        for team in teams
    ]
    team_aliases = _team_alias_index(
        teams,
        team_alias_evidence_by_key=team_alias_evidence_by_key,
    )
    stage_seconds["team_alias_evidence_load"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    roster_evidence = _load_roster_evidence(roster_evidence_path)
    roster_evidence_by_league = _roster_evidence_by_league(roster_evidence)
    stage_seconds["roster_evidence_load"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    manual_disambiguation_evidence = _load_manual_disambiguation_evidence(
        manual_disambiguation_path
    )
    manual_disambiguation_by_ticker = _manual_disambiguation_by_ticker(
        manual_disambiguation_evidence
    )
    stage_seconds["manual_disambiguation_load"] = time.perf_counter() - stage_started

    upgraded = 0
    existing_verified = 0
    team_alias_upgraded = 0
    team_alias_existing_verified = 0
    manual_upgraded = 0
    manual_existing_verified = 0
    roster_upgraded = 0
    roster_existing_verified = 0
    ambiguous = 0
    no_market = 0
    no_verified_game = 0
    no_verified_match = 0
    features_created = 0
    features_existing = 0
    rows: list[dict[str, Any]] = []
    manual_disambiguation_candidates: list[dict[str, Any]] = []

    _emit_progress(
        progress_callback,
        progress_every=progress_every,
        processed=0,
        total=len(partial_links),
        status="START",
        ticker=None,
        upgraded=upgraded,
        existing_verified=existing_verified,
        ambiguous=ambiguous,
        no_market=no_market,
        no_verified_game=no_verified_game,
        no_verified_match=no_verified_match,
        features_created=features_created,
        features_existing=features_existing,
        force=True,
    )

    stage_started = time.perf_counter()
    for processed, link in enumerate(partial_links, start=1):
        market = markets_by_ticker.get(link.ticker)
        if market is None:
            no_market += 1
            rows.append(_row(link, status="NO_MARKET", reason="Market row is missing."))
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=processed,
                total=len(partial_links),
                status="NO_MARKET",
                ticker=link.ticker,
                upgraded=upgraded,
                existing_verified=existing_verified,
                ambiguous=ambiguous,
                no_market=no_market,
                no_verified_game=no_verified_game,
                no_verified_match=no_verified_match,
                features_created=features_created,
                features_existing=features_existing,
            )
            continue
        candidate_games = _candidate_games(
            link,
            verified_games,
            games_by_league=verified_games_by_league,
        )
        if not candidate_games:
            no_verified_game += 1
            rows.append(
                _row(
                    link,
                    status="NO_VERIFIED_GAME",
                    reason="No verified ingested schedule games exist for this league.",
                )
            )
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=processed,
                total=len(partial_links),
                status="NO_VERIFIED_GAME",
                ticker=link.ticker,
                upgraded=upgraded,
                existing_verified=existing_verified,
                ambiguous=ambiguous,
                no_market=no_market,
                no_verified_game=no_verified_game,
                no_verified_match=no_verified_match,
                features_created=features_created,
                features_existing=features_existing,
            )
            continue
        matches = _verified_matches(
            market,
            link=link,
            games=candidate_games,
            teams=team_by_key,
            team_rows=team_rows,
            team_aliases=team_aliases,
            team_alias_evidence_by_key=team_alias_evidence_by_key,
            roster_evidence_by_league=roster_evidence_by_league,
            manual_disambiguation=manual_disambiguation_by_ticker.get(link.ticker),
            market_legs=market_legs_by_ticker.get(link.ticker, []),
            min_confidence=threshold,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
        if not matches:
            no_verified_match += 1
            rows.append(
                _row(
                    link,
                    status="NO_VERIFIED_MATCH",
                    reason=(
                        "Verified schedules exist, but no team/time/type match "
                        "cleared threshold."
                    ),
                    candidate_count=len(candidate_games),
                )
            )
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=processed,
                total=len(partial_links),
                status="NO_VERIFIED_MATCH",
                ticker=link.ticker,
                upgraded=upgraded,
                existing_verified=existing_verified,
                ambiguous=ambiguous,
                no_market=no_market,
                no_verified_game=no_verified_game,
                no_verified_match=no_verified_match,
                features_created=features_created,
                features_existing=features_existing,
            )
            continue
        top = matches[0]
        if _ambiguous(matches):
            ambiguous += 1
            candidate_rows = _match_candidate_rows(matches, teams=team_by_key)
            manual_disambiguation_candidates.append(
                _manual_disambiguation_candidate_row(
                    market,
                    link=link,
                    top_match=top,
                    candidate_games=candidate_rows,
                )
            )
            rows.append(
                _row(
                    link,
                    status="AMBIGUOUS_MATCH",
                    reason="Multiple verified games scored too closely to pick safely.",
                    verified_game_key=top.game.game_key,
                    confidence=top.confidence,
                    matched_terms=top.matched_terms,
                    candidate_count=len(matches),
                    candidate_games=candidate_rows,
                )
            )
            _emit_progress(
                progress_callback,
                progress_every=progress_every,
                processed=processed,
                total=len(partial_links),
                status="AMBIGUOUS_MATCH",
                ticker=link.ticker,
                upgraded=upgraded,
                existing_verified=existing_verified,
                ambiguous=ambiguous,
                no_market=no_market,
                no_verified_game=no_verified_game,
                no_verified_match=no_verified_match,
                features_created=features_created,
                features_existing=features_existing,
            )
            continue

        link_row, was_created = insert_sports_market_link(
            session,
            ticker=link.ticker,
            league=top.game.league,
            game_key=top.game.game_key,
            market_type=top.market_type,
            link_confidence=top.confidence,
            link_reason=f"Phase 3AE verified schedule/team match. {top.reason}",
            matched_terms=sorted(
                set(
                    [
                        *top.matched_terms,
                        VERIFIED_SOURCE,
                        *([TEAM_ALIAS_EVIDENCE_SOURCE] if top.team_alias_evidence else []),
                        *([ROSTER_EVIDENCE_SOURCE] if top.roster_evidence else []),
                        *([MANUAL_DISAMBIGUATION_SOURCE] if top.manual_disambiguation else []),
                    ]
                )
            ),
            raw_json={
                "source": VERIFIED_SOURCE,
                "phase": "3AE",
                "phase_version": PHASE_3AE_VERSION,
                "match_source": _match_source(top),
                "market_ticker": link.ticker,
                "market_title": market.title,
                "partial_link_id": link.id,
                "partial_game_key": link.game_key,
                "verified_game_key": top.game.game_key,
                "classification": top.classification,
                "matched_terms": top.matched_terms,
                "team_alias_evidence": top.team_alias_evidence,
                "roster_evidence": top.roster_evidence,
                "manual_disambiguation": top.manual_disambiguation,
                "score_reason": top.reason,
                "partial_raw": decode_json(link.raw_json),
            },
        )
        upgraded += int(was_created)
        existing_verified += int(not was_created)
        if top.team_alias_evidence:
            team_alias_upgraded += int(was_created)
            team_alias_existing_verified += int(not was_created)
        if top.manual_disambiguation:
            manual_upgraded += int(was_created)
            manual_existing_verified += int(not was_created)
        if top.roster_evidence:
            roster_upgraded += int(was_created)
            roster_existing_verified += int(not was_created)
        feature_status = "skipped"
        if build_features:
            feature_status = _ensure_verified_feature(
                session,
                game=top.game,
                link=link_row,
                settings=resolved,
                refresh_features=refresh_features,
            )
            features_created += int(feature_status == "created")
            features_existing += int(feature_status == "existing")
        rows.append(
            _row(
                link,
                status="VERIFIED_LINK_CREATED" if was_created else "VERIFIED_LINK_EXISTS",
                reason=top.reason,
                verified_game_key=top.game.game_key,
                confidence=top.confidence,
                matched_terms=top.matched_terms,
                feature_status=feature_status,
            )
        )
        _emit_progress(
            progress_callback,
            progress_every=progress_every,
            processed=processed,
            total=len(partial_links),
            status="VERIFIED_LINK_CREATED" if was_created else "VERIFIED_LINK_EXISTS",
            ticker=link.ticker,
            upgraded=upgraded,
            existing_verified=existing_verified,
            ambiguous=ambiguous,
            no_market=no_market,
            no_verified_game=no_verified_game,
            no_verified_match=no_verified_match,
            features_created=features_created,
            features_existing=features_existing,
        )

    stage_seconds["matching_loop"] = time.perf_counter() - stage_started

    stage_started = time.perf_counter()
    after = _connector_provenance_snapshot(
        session,
        use_fast_snapshot=bool(normalized_candidate_game_keys),
    )
    stage_seconds["after_snapshot"] = time.perf_counter() - stage_started

    unresolved = ambiguous + no_market + no_verified_game + no_verified_match
    _emit_progress(
        progress_callback,
        progress_every=progress_every,
        processed=len(partial_links),
        total=len(partial_links),
        status="DONE",
        ticker=None,
        upgraded=upgraded,
        existing_verified=existing_verified,
        ambiguous=ambiguous,
        no_market=no_market,
        no_verified_game=no_verified_game,
        no_verified_match=no_verified_match,
        features_created=features_created,
        features_existing=features_existing,
        force=True,
    )
    total_elapsed = time.perf_counter() - total_started
    stage_seconds["total"] = total_elapsed
    performance = _performance_payload(
        stage_seconds,
        processed_links=len(partial_links),
        verified_games_seen=len(verified_games),
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AE",
        "phase_version": PHASE_3AE_VERSION,
        "mode": "PAPER_ONLY_VERIFIED_SPORTS_CONNECTOR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "min_confidence": str(threshold),
        "ambiguity_margin": str(AMBIGUITY_MARGIN),
        "max_schedule_delta_hours": max_schedule_delta_hours,
        "candidate_game_keys": sorted(normalized_candidate_game_keys),
        "roster_evidence_path": str(roster_evidence_path) if roster_evidence_path else None,
        "team_alias_review_path": str(team_alias_review_path)
        if team_alias_review_path
        else None,
        "manual_disambiguation_path": str(manual_disambiguation_path)
        if manual_disambiguation_path
        else None,
        "before": before,
        "after": after,
        "summary": {
            "partial_links_reviewed": len(partial_links),
            "verified_games_seen": len(verified_games),
            "candidate_game_key_filter_count": len(normalized_candidate_game_keys),
            "team_alias_evidence_rows_seen": len(team_alias_evidence),
            "team_alias_evidence_rows_applied": sum(
                len(rows) for rows in team_alias_evidence_by_key.values()
            ),
            "roster_evidence_rows_seen": len(roster_evidence),
            "manual_disambiguation_rows_seen": len(manual_disambiguation_evidence),
            "manual_disambiguation_rows_applied": len(manual_disambiguation_by_ticker),
            "verified_links_created": upgraded,
            "verified_links_existing": existing_verified,
            "team_alias_verified_links_created": team_alias_upgraded,
            "team_alias_verified_links_existing": team_alias_existing_verified,
            "roster_verified_links_created": roster_upgraded,
            "roster_verified_links_existing": roster_existing_verified,
            "manual_disambiguation_links_created": manual_upgraded,
            "manual_disambiguation_links_existing": manual_existing_verified,
            "manual_disambiguation_candidates": len(manual_disambiguation_candidates),
            "features_created": features_created,
            "features_existing": features_existing,
            "unresolved": unresolved,
            "ambiguous_matches": ambiguous,
            "missing_market_rows": no_market,
            "no_verified_game": no_verified_game,
            "no_verified_match": no_verified_match,
            "remaining_partial_without_upgrade": after["partial_without_upgrade"],
        },
        "performance": performance,
        "rows": rows,
        "manual_disambiguation_candidates": manual_disambiguation_candidates,
        "recommended_next_action": _next_action(
            verified_games=len(verified_games),
            created=upgraded,
            unresolved=unresolved,
            remaining=after["partial_without_upgrade"],
        ),
    }


def write_phase3ae_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ae"),
    settings: Settings | None = None,
    limit: int | None = None,
    candidate_game_keys: set[str] | None = None,
    min_confidence: Decimal | None = None,
    build_features: bool = True,
    refresh_features: bool = False,
    max_schedule_delta_hours: int | None = DEFAULT_MAX_SCHEDULE_DELTA_HOURS,
    roster_evidence_path: Path | None = DEFAULT_ROSTER_EVIDENCE_PATH,
    team_alias_review_path: Path | None = DEFAULT_TEAM_ALIAS_REVIEW_PATH,
    manual_disambiguation_path: Path | None = DEFAULT_MANUAL_DISAMBIGUATION_PATH,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_every: int = 0,
) -> Phase3AEArtifactSet:
    payload = run_verified_sports_schedule_connector(
        session,
        settings=settings,
        limit=limit,
        candidate_game_keys=candidate_game_keys,
        min_confidence=min_confidence,
        build_features=build_features,
        refresh_features=refresh_features,
        max_schedule_delta_hours=max_schedule_delta_hours,
        roster_evidence_path=roster_evidence_path,
        team_alias_review_path=team_alias_review_path,
        manual_disambiguation_path=manual_disambiguation_path,
        progress_callback=progress_callback,
        progress_every=progress_every,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ae_verified_sports_connector.json"
    markdown_path = output_dir / "phase3ae_verified_sports_connector.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AEArtifactSet(output_dir, json_path, markdown_path)


def _markets_by_ticker(session: Session, tickers: list[str]) -> dict[str, Market]:
    unique = sorted({ticker for ticker in tickers if ticker})
    markets: dict[str, Market] = {}
    for index in range(0, len(unique), 500):
        batch = unique[index : index + 500]
        markets.update(
            {
                market.ticker: market
                for market in session.scalars(select(Market).where(Market.ticker.in_(batch)))
            }
        )
    return markets


def _market_legs_by_ticker(session: Session, tickers: list[str]) -> dict[str, list[MarketLeg]]:
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


def _connector_provenance_snapshot(
    session: Session,
    *,
    use_fast_snapshot: bool,
) -> dict[str, Any]:
    if not use_fast_snapshot:
        return build_sports_provenance_snapshot(session)
    session.flush()
    kalshi_condition = _kalshi_event_link_condition()
    partial_condition = _partial_market_link_condition()
    total_links = int(session.scalar(select(func.count()).select_from(SportsMarketLink)) or 0)
    kalshi_count = _count_links(session, kalshi_condition)
    partial_count = _count_links(session, ~kalshi_condition & partial_condition)
    verified_count = max(0, total_links - kalshi_count - partial_count)
    partial_tickers = set(
        session.scalars(
            select(SportsMarketLink.ticker)
            .where(~kalshi_condition & partial_condition)
            .distinct()
        )
    )
    upgraded_tickers = set(
        session.scalars(
            select(SportsMarketLink.ticker)
            .where(kalshi_condition | ~partial_condition)
            .distinct()
        )
    )
    return {
        "parsed_sports_markets": int(
            session.scalar(
                select(func.count(func.distinct(MarketLeg.ticker))).where(
                    MarketLeg.category == "sports"
                )
            )
            or 0
        ),
        "sports_links": total_links,
        "sports_games": int(session.scalar(select(func.count()).select_from(SportsGame)) or 0),
        "sports_features": int(
            session.scalar(select(func.count()).select_from(SportsFeature)) or 0
        ),
        "provenance_counts": {
            VERIFIED_SOURCE: verified_count,
            "kalshi_event_derived": kalshi_count,
            "partial_market_derived": partial_count,
        },
        "partial_without_upgrade": len(partial_tickers - upgraded_tickers),
        "partial_examples": _snapshot_examples(
            session,
            ~kalshi_condition & partial_condition,
        ),
        "derived_examples": _snapshot_examples(session, kalshi_condition),
        "snapshot_mode": "fast_sql_aggregate",
    }


def _kalshi_event_link_condition() -> Any:
    return or_(
        SportsMarketLink.game_key.contains("kalshi-event-derived"),
        SportsMarketLink.raw_json.contains("kalshi_event_derived"),
    )


def _partial_market_link_condition() -> Any:
    return or_(
        SportsMarketLink.game_key.contains("market-derived"),
        SportsMarketLink.raw_json.contains("market-derived-fallback"),
        SportsMarketLink.link_reason.contains("market-derived"),
    )


def _count_links(session: Session, condition: Any) -> int:
    return int(
        session.scalar(select(func.count()).select_from(SportsMarketLink).where(condition)) or 0
    )


def _snapshot_examples(
    session: Session,
    condition: Any,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    rows = []
    for link in session.scalars(
        select(SportsMarketLink)
        .where(condition)
        .order_by(SportsMarketLink.created_at, SportsMarketLink.id)
        .limit(limit)
    ):
        rows.append(
            {
                "ticker": link.ticker,
                "league": link.league,
                "game_key": link.game_key,
                "market_type": link.market_type,
                "link_confidence": link.link_confidence,
            }
        )
    return rows


def _partial_links_without_verified_link(
    session: Session,
    *,
    limit: int | None,
) -> list[SportsMarketLink]:
    kalshi_condition = _kalshi_event_link_condition()
    partial_condition = _partial_market_link_condition()
    verified_ticker_subquery = (
        select(SportsMarketLink.ticker)
        .where(~kalshi_condition & ~partial_condition)
        .distinct()
    )
    query = (
        select(SportsMarketLink)
        .where(
            ~kalshi_condition & partial_condition,
            SportsMarketLink.ticker.not_in(verified_ticker_subquery),
        )
        .order_by(SportsMarketLink.created_at, SportsMarketLink.id)
    )
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def _emit_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    progress_every: int,
    processed: int,
    total: int,
    status: str,
    ticker: str | None,
    upgraded: int,
    existing_verified: int,
    ambiguous: int,
    no_market: int,
    no_verified_game: int,
    no_verified_match: int,
    features_created: int,
    features_existing: int,
    force: bool = False,
) -> None:
    if progress_callback is None:
        return
    cadence = max(progress_every, 0)
    if not force and (cadence == 0 or processed % cadence != 0):
        return
    progress_callback(
        {
            "phase": "3AE",
            "status": status,
            "processed": processed,
            "total": total,
            "ticker": ticker,
            "upgraded": upgraded,
            "existing_verified": existing_verified,
            "ambiguous": ambiguous,
            "no_market": no_market,
            "no_verified_game": no_verified_game,
            "no_verified_match": no_verified_match,
            "unresolved": ambiguous + no_market + no_verified_game + no_verified_match,
            "features_created": features_created,
            "features_existing": features_existing,
        }
    )


def _normalized_candidate_game_keys(values: set[str] | None) -> set[str]:
    return {str(value).strip() for value in values or set() if str(value).strip()}


def _verified_games(
    session: Session,
    *,
    candidate_game_keys: set[str] | None = None,
) -> list[SportsGame]:
    normalized = _normalized_candidate_game_keys(candidate_game_keys)
    if normalized:
        games = list(
            session.scalars(
                select(SportsGame)
                .where(SportsGame.game_key.in_(sorted(normalized)))
                .order_by(SportsGame.league, SportsGame.scheduled_at, SportsGame.game_key)
            )
        )
        return [game for game in games if _game_is_verified(game)]
    games = [game for game in sports_games(session, league="ALL") if _game_is_verified(game)]
    return games


def _connector_teams(
    session: Session,
    *,
    verified_games: list[SportsGame],
    use_candidate_team_index: bool,
) -> list[SportsTeam]:
    if not use_candidate_team_index:
        return sports_teams(session, league="ALL")
    team_keys = sorted(
        {
            str(team_key)
            for game in verified_games
            for team_key in (game.home_team_key, game.away_team_key)
            if team_key
        }
    )
    if not team_keys:
        return []
    return list(
        session.scalars(
            select(SportsTeam)
            .where(SportsTeam.team_key.in_(team_keys))
            .order_by(SportsTeam.league, SportsTeam.team_name)
        )
    )


def _games_by_league(games: list[SportsGame]) -> dict[str, list[SportsGame]]:
    by_league: dict[str, list[SportsGame]] = {}
    for game in games:
        by_league.setdefault(str(game.league or "").upper(), []).append(game)
    return by_league


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


def _has_round_placeholder_team(game: SportsGame) -> bool:
    return any(
        _is_round_placeholder_team_key(team_key)
        for team_key in (game.home_team_key, game.away_team_key)
    )


def _is_round_placeholder_team_key(value: object) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    slug = text.split(":", 1)[-1]
    if slug == "sf":
        return False
    return bool(ROUND_PLACEHOLDER_TEAM_RE.search(slug))


def _candidate_games(
    link: SportsMarketLink,
    games: list[SportsGame],
    *,
    games_by_league: dict[str, list[SportsGame]],
) -> list[SportsGame]:
    league = str(link.league or "").upper()
    if league in {"", "ALL", "SPORTS", UNKNOWN}:
        return games
    return games_by_league.get(league, [])


def _verified_matches(
    market: Market,
    *,
    link: SportsMarketLink,
    games: list[SportsGame],
    teams: dict[str, SportsTeam],
    team_rows: list[dict[str, Any]],
    team_aliases: list[tuple[str, list[str]]],
    team_alias_evidence_by_key: dict[str, list[VerifiedTeamAliasEvidence]],
    roster_evidence_by_league: dict[str, list[VerifiedRosterEvidence]],
    manual_disambiguation: ManualDisambiguationEvidence | None,
    market_legs: list[MarketLeg],
    min_confidence: Decimal,
    max_schedule_delta_hours: int | None,
) -> list[VerifiedSportsMatch]:
    window_games = [
        game
        for game in games
        if _schedule_window_allowed(
            market,
            game,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
    ]
    if not window_games:
        return []
    classification = classify_sports_market(market, teams=team_rows)
    classification = _classification_with_link_defaults(classification, link)
    mentioned_team_keys = _mentioned_team_keys(market, team_aliases)
    matches_by_game: dict[str, VerifiedSportsMatch] = {}
    for game in window_games:
        if _has_round_placeholder_team(game):
            continue
        manual_match = _manual_disambiguation_match(
            market,
            link=link,
            game=game,
            classification=classification,
            evidence=manual_disambiguation,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
        if manual_match is not None and manual_match.confidence >= min_confidence:
            _add_match(matches_by_game, manual_match)
        if _has_team_conflict(game, mentioned_team_keys=mentioned_team_keys):
            continue
        confidence, reason, matched_terms, market_type = score_sports_market_link(
            market,
            game,
            home_team=teams.get(game.home_team_key),
            away_team=teams.get(game.away_team_key),
            classification=classification,
        )
        confidence, reason, matched_terms, alias_payloads = _apply_team_alias_evidence_score(
            confidence,
            reason,
            matched_terms,
            text=str(classification.get("text") or ""),
            game=game,
            team_alias_evidence_by_key=team_alias_evidence_by_key,
        )
        if confidence >= min_confidence:
            _add_match(
                matches_by_game,
                VerifiedSportsMatch(
                    game=game,
                    confidence=confidence,
                    reason=reason,
                    matched_terms=matched_terms,
                    market_type=market_type if market_type != UNKNOWN else link.market_type,
                    classification=classification,
                    team_alias_evidence=alias_payloads,
                    roster_evidence=[],
                    manual_disambiguation=None,
                ),
            )
        roster_match = _player_prop_roster_match(
            market,
            link=link,
            game=game,
            classification=classification,
            mentioned_team_keys=mentioned_team_keys,
            roster_evidence=roster_evidence_by_league.get(str(game.league or "").upper(), []),
            market_legs=market_legs,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
        if roster_match is not None and roster_match.confidence >= min_confidence:
            _add_match(matches_by_game, roster_match)
    matches = list(matches_by_game.values())
    return sorted(matches, key=lambda row: (-row.confidence, row.game.game_key))


def _add_match(
    matches_by_game: dict[str, VerifiedSportsMatch],
    match: VerifiedSportsMatch,
) -> None:
    existing = matches_by_game.get(match.game.game_key)
    if existing is None or match.confidence > existing.confidence:
        matches_by_game[match.game.game_key] = match
        return
    if existing.confidence == match.confidence:
        matches_by_game[match.game.game_key] = VerifiedSportsMatch(
            game=existing.game,
            confidence=existing.confidence,
            reason=f"{existing.reason} {match.reason}",
            matched_terms=sorted(set([*existing.matched_terms, *match.matched_terms])),
            market_type=existing.market_type
            if existing.market_type != UNKNOWN
            else match.market_type,
            classification=existing.classification,
            team_alias_evidence=[*existing.team_alias_evidence, *match.team_alias_evidence],
            roster_evidence=[*existing.roster_evidence, *match.roster_evidence],
            manual_disambiguation=existing.manual_disambiguation or match.manual_disambiguation,
        )


def _player_prop_roster_match(
    market: Market,
    *,
    link: SportsMarketLink,
    game: SportsGame,
    classification: dict[str, Any],
    mentioned_team_keys: set[str],
    roster_evidence: list[VerifiedRosterEvidence],
    market_legs: list[MarketLeg],
    max_schedule_delta_hours: int | None,
) -> VerifiedSportsMatch | None:
    market_type = str(classification.get("market_type") or link.market_type or UNKNOWN).upper()
    if market_type != "PLAYER_PROP":
        return None
    if not _has_clean_schedule_time(
        market,
        game,
        max_schedule_delta_hours=max_schedule_delta_hours,
    ):
        return None
    text = str(classification.get("text") or "").lower()
    player_prop_entities = _player_prop_leg_entities(market_legs)
    mentioned = [
        evidence
        for evidence in roster_evidence
        if (
            _phrase_in_text(text, evidence.player_name)
            or any(_phrase_in_text(entity, evidence.player_name) for entity in player_prop_entities)
        )
        and _evidence_valid_for_game(evidence, market=market, game=game)
    ]
    if not mentioned:
        return None
    if player_prop_entities and any(
        not any(_phrase_in_text(entity, evidence.player_name) for evidence in mentioned)
        for entity in player_prop_entities
    ):
        return None
    game_team_keys = {_normalized_key(game.home_team_key), _normalized_key(game.away_team_key)}
    evidence_team_keys = {_normalized_key(evidence.current_team_key) for evidence in mentioned}
    if not evidence_team_keys <= game_team_keys:
        return None
    normalized_mentions = {_normalized_key(team_key) for team_key in mentioned_team_keys}
    if normalized_mentions and not normalized_mentions <= game_team_keys:
        return None
    matched_terms = sorted(
        {
            "game_time",
            "player_prop",
            "verified_roster",
            *[evidence.player_name.lower() for evidence in mentioned],
            *[evidence.current_team_key.lower() for evidence in mentioned],
        }
    )
    evidence_rows = [_roster_evidence_payload(evidence) for evidence in mentioned]
    return VerifiedSportsMatch(
        game=game,
        confidence=Decimal("0.9500"),
        reason=(
            "Player prop matched verified roster evidence, roster team, schedule "
            "window, and market type."
        ),
        matched_terms=matched_terms,
        market_type="PLAYER_PROP",
        classification=classification,
        team_alias_evidence=[],
        roster_evidence=evidence_rows,
        manual_disambiguation=None,
    )


def _manual_disambiguation_match(
    market: Market,
    *,
    link: SportsMarketLink,
    game: SportsGame,
    classification: dict[str, Any],
    evidence: ManualDisambiguationEvidence | None,
    max_schedule_delta_hours: int | None,
) -> VerifiedSportsMatch | None:
    if evidence is None:
        return None
    if evidence.chosen_game_key != game.game_key:
        return None
    if evidence.league and evidence.league != str(game.league or "").upper():
        return None
    if not _has_clean_schedule_time(
        market,
        game,
        max_schedule_delta_hours=max_schedule_delta_hours,
    ):
        return None
    market_type = (
        evidence.chosen_market_type
        or str(classification.get("market_type") or "")
        or str(link.market_type or "")
    ).upper()
    if not market_type or market_type == UNKNOWN:
        return None
    payload = _manual_disambiguation_payload(evidence)
    return VerifiedSportsMatch(
        game=game,
        confidence=Decimal("0.9000"),
        reason=(
            "Approved Phase 3AH manual disambiguation selected one verified game, "
            "market type, and schedule window."
        ),
        matched_terms=sorted(
            {
                "game_time",
                "manual_disambiguation",
                market_type.lower(),
                str(game.home_team_key or "").lower(),
                str(game.away_team_key or "").lower(),
            }
        ),
        market_type=market_type,
        classification=classification,
        team_alias_evidence=[],
        roster_evidence=[],
        manual_disambiguation=payload,
    )


def _apply_team_alias_evidence_score(
    confidence: Decimal,
    reason: str,
    matched_terms: list[str],
    *,
    text: str,
    game: SportsGame,
    team_alias_evidence_by_key: dict[str, list[VerifiedTeamAliasEvidence]],
) -> tuple[Decimal, str, list[str], list[dict[str, Any]]]:
    normalized_text = str(text or "").lower()
    alias_payloads: list[dict[str, Any]] = []
    alias_terms: set[str] = set()
    extra_score = Decimal("0")
    for team_key in (game.home_team_key, game.away_team_key):
        for evidence in team_alias_evidence_by_key.get(str(team_key or ""), []):
            alias = evidence.alias.lower()
            if alias and _alias_in_text(normalized_text, alias):
                alias_payloads.append(_team_alias_evidence_payload(evidence))
                alias_terms.add(alias)
                extra_score += Decimal("0.25")
                break
    if not alias_payloads:
        return confidence, reason, matched_terms, []
    boosted = min(confidence + extra_score, Decimal("1.00")).quantize(Decimal("0.0001"))
    updated_reason = f"{reason} Approved team alias evidence matched."
    updated_terms = sorted(
        set([*matched_terms, TEAM_ALIAS_EVIDENCE_SOURCE, *alias_terms])
    )
    return boosted, updated_reason, updated_terms, alias_payloads


def _player_prop_leg_entities(market_legs: list[MarketLeg]) -> list[str]:
    entities: list[str] = []
    for leg in market_legs:
        if str(leg.market_type or "").upper() != "PLAYER_PROP":
            continue
        entity = _text(leg.entity_name)
        if entity:
            entities.append(entity)
    return sorted(set(entities))


def _schedule_window_allowed(
    market: Market,
    game: SportsGame,
    *,
    max_schedule_delta_hours: int | None,
) -> bool:
    if max_schedule_delta_hours is None or max_schedule_delta_hours <= 0:
        return True
    if market.close_time is None or game.scheduled_at is None:
        return True
    market_close = market.close_time
    scheduled = game.scheduled_at
    if (market_close.tzinfo is None) != (scheduled.tzinfo is None):
        market_close = market_close.replace(tzinfo=None)
        scheduled = scheduled.replace(tzinfo=None)
    return abs(market_close - scheduled) <= timedelta(hours=max_schedule_delta_hours)


def _has_clean_schedule_time(
    market: Market,
    game: SportsGame,
    *,
    max_schedule_delta_hours: int | None,
) -> bool:
    if max_schedule_delta_hours is not None and max_schedule_delta_hours > 0:
        if market.close_time is None or game.scheduled_at is None:
            return False
    return _schedule_window_allowed(
        market,
        game,
        max_schedule_delta_hours=max_schedule_delta_hours,
    )


def _load_roster_evidence(path: Path | None) -> list[VerifiedRosterEvidence]:
    if path is None or not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = (
        payload.get("verified_roster_evidence", payload)
        if isinstance(payload, dict)
        else payload
    )
    evidence_rows: list[VerifiedRosterEvidence] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if row.get("safe_to_apply") is not True:
            continue
        if str(row.get("review_status") or "").upper() not in {
            "APPROVED",
            "READY",
            "REVIEWED_VERIFIED",
            "VERIFIED",
        }:
            continue
        player_name = _text(row.get("player_name"))
        current_team_key = _text(row.get("current_team_key"))
        league = _text(row.get("league")).upper()
        if not player_name or not current_team_key or not league:
            continue
        evidence_rows.append(
            VerifiedRosterEvidence(
                league=league,
                player_name=player_name,
                canonical_player_id=_text(row.get("canonical_player_id")),
                current_team_key=current_team_key,
                current_team_name=_text(row.get("current_team_name")),
                roster_source_url=_text(row.get("roster_source_url")),
                valid_from=_date_or_none(row.get("valid_from")),
                valid_to=_date_or_none(row.get("valid_to")),
                raw=dict(row),
            )
        )
    return evidence_rows


def _load_team_alias_evidence(path: Path | None) -> list[VerifiedTeamAliasEvidence]:
    if path is None or not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = (
        payload.get("team_alias_review_template", payload.get("rows", payload))
        if isinstance(payload, dict)
        else payload
    )
    evidence_rows: list[VerifiedTeamAliasEvidence] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if row.get("safe_to_apply") is not True:
            continue
        if not _approved_review_status(row):
            continue
        alias = _text(row.get("alias_to_add") or row.get("entity"))
        league = _text(row.get("league")).upper()
        canonical_team_key = _review_team_key(row.get("canonical_team_key"), league=league)
        evidence_source_url = _text(row.get("evidence_source_url"))
        if not alias or len(alias) < 3 or not league or not canonical_team_key:
            continue
        if not evidence_source_url.startswith(("http://", "https://")):
            continue
        evidence_rows.append(
            VerifiedTeamAliasEvidence(
                league=league,
                alias=alias,
                canonical_team_key=canonical_team_key,
                canonical_team_name=_text(row.get("canonical_team_name")),
                evidence_source_url=evidence_source_url,
                raw=dict(row),
            )
        )
    return evidence_rows


def _team_alias_evidence_by_key(
    evidence_rows: list[VerifiedTeamAliasEvidence],
    *,
    team_by_key: dict[str, SportsTeam],
) -> dict[str, list[VerifiedTeamAliasEvidence]]:
    by_key: dict[str, list[VerifiedTeamAliasEvidence]] = {}
    for row in evidence_rows:
        if row.canonical_team_key not in team_by_key:
            continue
        if _is_round_placeholder_team_key(row.canonical_team_key):
            continue
        by_key.setdefault(row.canonical_team_key, []).append(row)
    return by_key


def _load_manual_disambiguation_evidence(
    path: Path | None,
) -> list[ManualDisambiguationEvidence]:
    if path is None or not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = (
        payload.get("manual_disambiguation_template", payload.get("rows", payload))
        if isinstance(payload, dict)
        else payload
    )
    evidence_rows: list[ManualDisambiguationEvidence] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if row.get("safe_to_upgrade") is not True:
            continue
        if not _approved_review_status(row):
            continue
        ticker = _text(row.get("ticker")).upper()
        league = _text(row.get("league")).upper()
        chosen_game_key = _text(row.get("chosen_game_key"))
        chosen_market_type = _text(row.get("chosen_market_type")).upper()
        verification_source_url = _text(row.get("verification_source_url"))
        if not ticker or not league or not chosen_game_key or not chosen_market_type:
            continue
        if not verification_source_url.startswith(("http://", "https://")):
            continue
        evidence_rows.append(
            ManualDisambiguationEvidence(
                ticker=ticker,
                league=league,
                chosen_game_key=chosen_game_key,
                chosen_market_type=chosen_market_type,
                verification_source_url=verification_source_url,
                raw=dict(row),
            )
        )
    return evidence_rows


def _manual_disambiguation_by_ticker(
    evidence_rows: list[ManualDisambiguationEvidence],
) -> dict[str, ManualDisambiguationEvidence]:
    grouped: dict[str, list[ManualDisambiguationEvidence]] = {}
    for row in evidence_rows:
        grouped.setdefault(row.ticker, []).append(row)
    by_ticker: dict[str, ManualDisambiguationEvidence] = {}
    for ticker, rows in grouped.items():
        chosen_keys = {row.chosen_game_key for row in rows}
        if len(chosen_keys) == 1:
            by_ticker[ticker] = rows[0]
    return by_ticker


def _roster_evidence_by_league(
    evidence_rows: list[VerifiedRosterEvidence],
) -> dict[str, list[VerifiedRosterEvidence]]:
    by_league: dict[str, list[VerifiedRosterEvidence]] = {}
    for row in evidence_rows:
        by_league.setdefault(row.league, []).append(row)
    return by_league


def _evidence_valid_for_game(
    evidence: VerifiedRosterEvidence,
    *,
    market: Market,
    game: SportsGame,
) -> bool:
    reference = game.scheduled_at or market.close_time
    if reference is None:
        return False
    reference_date = reference.date()
    if evidence.valid_from is None:
        return False
    if evidence.valid_from > reference_date:
        return False
    return not (evidence.valid_to is not None and evidence.valid_to < reference_date)


def _phrase_in_text(text: str, phrase: str) -> bool:
    normalized_text = " ".join(text.lower().replace("-", " ").split())
    normalized_phrase = " ".join(str(phrase or "").lower().replace("-", " ").split())
    if not normalized_phrase:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(normalized_phrase)}(?![a-z0-9])"
    return re.search(pattern, normalized_text) is not None


def _normalized_key(value: object) -> str:
    return str(value or "").strip().lower()


def _roster_evidence_payload(evidence: VerifiedRosterEvidence) -> dict[str, Any]:
    return {
        "league": evidence.league,
        "player_name": evidence.player_name,
        "canonical_player_id": evidence.canonical_player_id,
        "current_team_key": evidence.current_team_key,
        "current_team_name": evidence.current_team_name,
        "roster_source_url": evidence.roster_source_url,
        "valid_from": evidence.valid_from.isoformat() if evidence.valid_from else "",
        "valid_to": evidence.valid_to.isoformat() if evidence.valid_to else "",
        "evidence_id": evidence.raw.get("evidence_id"),
    }


def _team_alias_evidence_payload(evidence: VerifiedTeamAliasEvidence) -> dict[str, Any]:
    return {
        "league": evidence.league,
        "alias": evidence.alias,
        "canonical_team_key": evidence.canonical_team_key,
        "canonical_team_name": evidence.canonical_team_name,
        "evidence_source_url": evidence.evidence_source_url,
    }


def _manual_disambiguation_payload(evidence: ManualDisambiguationEvidence) -> dict[str, Any]:
    return {
        "ticker": evidence.ticker,
        "league": evidence.league,
        "chosen_game_key": evidence.chosen_game_key,
        "chosen_market_type": evidence.chosen_market_type,
        "verification_source_url": evidence.verification_source_url,
    }


def _match_candidate_rows(
    matches: list[VerifiedSportsMatch],
    *,
    teams: dict[str, SportsTeam],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in matches:
        game = match.game
        rows.append(
            {
                "game_key": game.game_key,
                "league": game.league,
                "scheduled_at": game.scheduled_at.isoformat()
                if game.scheduled_at
                else "",
                "home_team": _team_label(game.home_team_key, teams=teams),
                "away_team": _team_label(game.away_team_key, teams=teams),
                "market_type": match.market_type,
                "confidence": str(match.confidence),
                "matched_terms": match.matched_terms,
                "reason": match.reason,
                "clean": False,
            }
        )
    return rows


def _manual_disambiguation_candidate_row(
    market: Market,
    *,
    link: SportsMarketLink,
    top_match: VerifiedSportsMatch,
    candidate_games: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ticker": link.ticker,
        "league": link.league,
        "market_type": top_match.market_type,
        "market_close_time": market.close_time.isoformat() if market.close_time else "",
        "market_title": market.title,
        "partial_game_key": link.game_key,
        "primary_cause": "PHASE3AE_AMBIGUOUS_VERIFIED_MATCH",
        "candidate_games": candidate_games,
        "review_status": "UNVERIFIED",
        "chosen_game_key": "",
        "chosen_market_type": "",
        "verification_source_url": "",
        "safe_to_upgrade": False,
        "safety_note": (
            "Do not mark safe unless exactly one candidate game and market type are "
            "verified from source evidence."
        ),
    }


def _team_label(team_key: str, *, teams: dict[str, SportsTeam]) -> str:
    team = teams.get(team_key)
    if team is None:
        return team_key
    return f"{team.team_name} ({team.team_key})"


def _date_or_none(value: object) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _text(value: object) -> str:
    return str(value or "").strip()


def _approved_review_status(row: dict[str, Any]) -> bool:
    return str(row.get("review_status") or "").upper() in APPROVED_REVIEW_STATUSES


def _review_team_key(value: object, *, league: str) -> str:
    text = _text(value)
    if not text or not league:
        return ""
    if ":" in text:
        raw_league, raw_key = text.split(":", 1)
        return f"{raw_league.upper()}:{_slug(raw_key)}"
    return f"{league}:{_slug(text)}"


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "unknown"


def _team_alias_index(
    teams: list[SportsTeam],
    *,
    team_alias_evidence_by_key: dict[str, list[VerifiedTeamAliasEvidence]] | None = None,
) -> list[tuple[str, list[str]]]:
    return [
        (
            team.team_key,
            _team_aliases(
                team,
                extra_aliases=team_alias_evidence_by_key.get(team.team_key, [])
                if team_alias_evidence_by_key
                else [],
            ),
        )
        for team in teams
    ]


def _mentioned_team_keys(
    market: Market,
    team_aliases: list[tuple[str, list[str]]],
) -> set[str]:
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
    mentioned: set[str] = set()
    for team_key, aliases in team_aliases:
        if any(alias and _alias_in_text(text, alias) for alias in aliases):
            mentioned.add(team_key)
    return mentioned


def _team_aliases(
    team: SportsTeam,
    *,
    extra_aliases: list[VerifiedTeamAliasEvidence] | None = None,
) -> list[str]:
    aliases = [alias for alias in sports_team_aliases(team) if len(alias) >= 3]
    aliases.extend(evidence.alias.lower() for evidence in extra_aliases or [])
    return sorted({alias for alias in aliases if len(alias) >= 3})


def _alias_in_text(text: str, alias: str) -> bool:
    if " " in alias:
        return alias in text
    padded = f" {text} "
    return any(f"{sep}{alias}{end}" in padded for sep in (" ", "-", ",") for end in (" ", ",", "."))


def _has_team_conflict(game: SportsGame, *, mentioned_team_keys: set[str]) -> bool:
    if not mentioned_team_keys:
        return False
    candidate_keys = {game.home_team_key, game.away_team_key}
    return bool(mentioned_team_keys - candidate_keys)


def _classification_with_link_defaults(
    classification: dict[str, Any],
    link: SportsMarketLink,
) -> dict[str, Any]:
    adjusted = dict(classification)
    if adjusted.get("league") == UNKNOWN and link.league not in {"ALL", "SPORTS", UNKNOWN}:
        adjusted["league"] = link.league
    if adjusted.get("market_type") == UNKNOWN and link.market_type != UNKNOWN:
        adjusted["market_type"] = link.market_type
    return adjusted


def _ambiguous(matches: list[VerifiedSportsMatch]) -> bool:
    if len(matches) < 2:
        return False
    top = matches[0].confidence
    runner_up = matches[1].confidence
    return top - runner_up < AMBIGUITY_MARGIN


def _ensure_verified_feature(
    session: Session,
    *,
    game: SportsGame,
    link: SportsMarketLink,
    settings: Settings,
    refresh_features: bool,
) -> str:
    existing = latest_sports_feature(
        session,
        ticker=link.ticker,
        league=game.league,
        game_key=game.game_key,
    )
    if existing is not None and not refresh_features:
        return "existing"
    payload = calculate_sports_feature(session, game, settings=settings)
    insert_sports_feature(
        session,
        league=game.league,
        game_key=game.game_key,
        ticker=link.ticker,
        home_team_key=game.home_team_key,
        away_team_key=game.away_team_key,
        team_strength_edge=payload["team_strength_edge"],
        injury_edge=payload["injury_edge"],
        rest_edge=payload["rest_edge"],
        travel_edge=payload["travel_edge"],
        odds_edge=payload["odds_edge"],
        weather_edge=payload["weather_edge"],
        total_edge=payload["total_edge"],
        home_win_probability=payload["home_win_probability"],
        away_win_probability=payload["away_win_probability"],
        projected_total=payload["projected_total"],
        confidence_score=payload["confidence_score"],
        raw_json={
            **payload,
            "source": "PHASE_3AE_VERIFIED_SCHEDULE",
            "link_id": link.id,
            "verified_game_key": game.game_key,
        },
    )
    return "created"


def _link_provenance(link: SportsMarketLink) -> str:
    raw = decode_json(link.raw_json)
    source = str(raw.get("source") or "").lower()
    reason = str(link.link_reason or "").lower()
    game_key = str(link.game_key or "").lower()
    if source == VERIFIED_SOURCE:
        return VERIFIED_SOURCE
    if "kalshi-event-derived" in game_key or source == "kalshi_event_derived":
        return "kalshi_event_derived"
    if (
        "market-derived" in game_key
        or source == "market-derived-fallback"
        or "market-derived" in reason
    ):
        return "partial_market_derived"
    return VERIFIED_SOURCE


def _match_source(match: VerifiedSportsMatch) -> str:
    if match.manual_disambiguation:
        return MANUAL_DISAMBIGUATION_SOURCE
    if match.roster_evidence:
        return ROSTER_EVIDENCE_SOURCE
    if match.team_alias_evidence:
        return TEAM_ALIAS_EVIDENCE_SOURCE
    return VERIFIED_SOURCE


def _team_mapping(
    team: SportsTeam,
    *,
    extra_aliases: list[VerifiedTeamAliasEvidence] | None = None,
) -> dict[str, Any]:
    return {
        "league": team.league,
        "team_key": team.team_key,
        "team_name": team.team_name,
        "abbreviation": team.abbreviation,
        "city": team.city,
        "aliases": [
            *sports_team_aliases(team),
            *[evidence.alias for evidence in extra_aliases or []],
        ],
    }


def _row(
    link: SportsMarketLink,
    *,
    status: str,
    reason: str,
    verified_game_key: str | None = None,
    confidence: Decimal | None = None,
    matched_terms: list[str] | None = None,
    candidate_count: int | None = None,
    candidate_games: list[dict[str, Any]] | None = None,
    feature_status: str | None = None,
) -> dict[str, Any]:
    return {
        "ticker": link.ticker,
        "status": status,
        "league": link.league,
        "partial_game_key": link.game_key,
        "market_type": link.market_type,
        "verified_game_key": verified_game_key,
        "confidence": str(confidence) if confidence is not None else None,
        "matched_terms": matched_terms or [],
        "candidate_count": candidate_count,
        "candidate_games": candidate_games or [],
        "feature_status": feature_status,
        "reason": reason,
    }


def _next_action(
    *,
    verified_games: int,
    created: int,
    unresolved: int,
    remaining: int,
) -> str:
    if verified_games == 0:
        return (
            "Bootstrap and ingest verified sports schedules first: "
            "kalshi-bot phase3af-sports-schedule-bootstrap "
            "--leagues MLB,WNBA,SOCCER --days-ahead 14 --ingest"
        )
    if created:
        return (
            "Run sports forecasts and market coverage after verified link upgrades: "
            "kalshi-bot forecast --model sports_v1 && "
            "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage"
        )
    if unresolved and remaining:
        return (
            "Verified schedules exist, but remaining partial links need better team aliases, "
            "more leagues, or disambiguation."
        )
    return "Sports schedule provenance is upgraded enough for paper-only model learning."


def _performance_payload(
    stage_seconds: dict[str, float],
    *,
    processed_links: int,
    verified_games_seen: int,
) -> dict[str, Any]:
    elapsed = stage_seconds.get("total", sum(stage_seconds.values()))
    stage_only = {key: value for key, value in stage_seconds.items() if key != "total"}
    slowest_stage = (
        max(stage_only.items(), key=lambda item: item[1])[0] if stage_only else "none"
    )
    return {
        "elapsed_seconds": round(elapsed, 3),
        "links_per_second": round(processed_links / elapsed, 3) if elapsed > 0 else None,
        "processed_links": processed_links,
        "verified_games_seen": verified_games_seen,
        "slowest_stage": slowest_stage,
        "stage_seconds": {
            key: round(value, 3) for key, value in sorted(stage_seconds.items())
        },
    }


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    performance = payload.get("performance", {})
    lines = [
        "# Phase 3AE Verified Sports Schedule Connector",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Min confidence: {payload['min_confidence']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    if performance:
        lines.extend(
            [
                "",
                "## Performance",
                "",
                f"- elapsed_seconds: {performance.get('elapsed_seconds')}",
                f"- links_per_second: {performance.get('links_per_second')}",
                f"- slowest_stage: {performance.get('slowest_stage')}",
                f"- stage_seconds: {performance.get('stage_seconds')}",
            ]
        )
    lines.extend(
        [
            "",
            "## Provenance Movement",
            "",
            f"- Before: {payload['before']['provenance_counts']}",
            f"- After: {payload['after']['provenance_counts']}",
            "",
            "## Link Review Rows",
            "",
            "| Ticker | Status | League | Partial game | Verified game | Confidence | Reason |",
            "| --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for row in payload["rows"][:50]:
        lines.append(
            f"| {row['ticker']} | {row['status']} | {row['league']} | "
            f"{row['partial_game_key']} | {row['verified_game_key'] or ''} | "
            f"{row['confidence'] or ''} | {_md(row['reason'])} |"
        )
    if not payload["rows"]:
        lines.append("| None |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Safety",
            "",
            "- Paper-only link and feature repair.",
            "- No demo orders.",
            "- No live orders.",
            "- Ambiguous schedule matches are not upgraded automatically.",
            "",
        ]
    )
    return "\n".join(lines)


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
