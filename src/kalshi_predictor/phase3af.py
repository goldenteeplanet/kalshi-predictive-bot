from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    Market,
    MarketSnapshot,
    SportsFeature,
    SportsGame,
    SportsMarketLink,
    SportsTeam,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.sports.classifier import UNKNOWN, classify_sports_market
from kalshi_predictor.sports.ingestion import SportsIngestionSummary, ingest_sports_payload
from kalshi_predictor.sports.linker import score_sports_market_link
from kalshi_predictor.sports.repository import (
    normalize_league,
    normalize_sports_status,
    sports_team_aliases_from_payload,
)
from kalshi_predictor.utils.decimals import midpoint, to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE_3AF_VERSION = "phase3af_v1"
ESPN_SOURCE = "espn_scoreboard"
AMBIGUITY_MARGIN = Decimal("0.0500")
DEFAULT_MAX_SCHEDULE_DELTA_HOURS = 18
DEFAULT_LEAGUES = ("MLB", "WNBA", "SOCCER")
DEFAULT_SOCCER_COMPETITIONS = (
    "fifa.world",
    "fifa.worldq",
    "fifa.worldq.concacaf",
    "fifa.worldq.conmebol",
    "fifa.worldq.uefa",
    "fifa.worldq.caf",
    "fifa.worldq.afc",
    "fifa.worldq.ofc",
    "fifa.friendly",
    "fifa.friendly.w",
    "concacaf.gold",
    "concacaf.nations.league",
    "eng.1",
    "esp.1",
    "ita.1",
    "ger.1",
    "fra.1",
    "usa.1",
    "mex.1",
    "bra.1",
    "arg.1",
    "uefa.champions",
    "uefa.europa",
    "uefa.europa.conf",
)

ESPN_LEAGUE_PATHS = {
    "MLB": ("baseball", "mlb"),
    "NBA": ("basketball", "nba"),
    "WNBA": ("basketball", "wnba"),
    "NFL": ("football", "nfl"),
    "NHL": ("hockey", "nhl"),
}


@dataclass(frozen=True)
class Phase3AFArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    schedule_paths: tuple[Path, ...]
    legacy_sample_path: Path | None


def run_sports_schedule_bootstrap(
    session: Session | None = None,
    *,
    leagues: str | list[str] | tuple[str, ...] = DEFAULT_LEAGUES,
    start_date: str | date | None = None,
    days_ahead: int = 7,
    schedule_output_dir: Path = Path("data/sports_schedules"),
    ingest: bool = False,
    write_legacy_sample: bool = True,
    timeout_seconds: float = 20.0,
    soccer_competitions: str | list[str] | tuple[str, ...] = DEFAULT_SOCCER_COMPETITIONS,
    include_coverage: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Fetch verified sports schedule payloads and optionally ingest them.

    This phase only reads public schedule data and writes local JSON/DB rows. It never touches
    order execution, demo execution, live execution, or paper trade creation.
    """
    if ingest and session is None:
        raise ValueError("A database session is required when ingest=True.")
    if days_ahead < 1:
        raise ValueError("days_ahead must be at least 1.")

    requested_leagues = _parse_leagues(leagues)
    competitions = _parse_csv(soccer_competitions)
    first_date = _parse_date(start_date)
    dates = [first_date + timedelta(days=offset) for offset in range(days_ahead)]
    schedule_output_dir.mkdir(parents=True, exist_ok=True)

    league_results: list[dict[str, Any]] = []
    schedule_paths: list[Path] = []
    legacy_sample_path: Path | None = None
    ingestion_summaries: list[dict[str, Any]] = []
    total_errors: list[str] = []

    for league in requested_leagues:
        payload, fetch_rows, errors = _build_league_payload(
            league=league,
            dates=dates,
            timeout_seconds=timeout_seconds,
            soccer_competitions=competitions,
        )
        total_errors.extend(errors)
        output_path = schedule_output_dir / _schedule_filename(league, first_date, days_ahead)
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        schedule_paths.append(output_path)
        if legacy_sample_path is None and write_legacy_sample:
            legacy_sample_path = Path("data/sports_sample.json")
            legacy_sample_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_sample_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        summary: SportsIngestionSummary | None = None
        if ingest and session is not None and payload["games"]:
            summary = ingest_sports_payload(
                session,
                payload,
                league=league,
                source=f"{ESPN_SOURCE}:phase3af",
            )
            ingestion_summaries.append(_ingestion_summary(summary))
            total_errors.extend(summary.errors)
        league_results.append(
            {
                "league": league,
                "source": ESPN_SOURCE,
                "output_path": str(output_path),
                "teams": len(payload["teams"]),
                "games": len(payload["games"]),
                "team_stats": len(payload["team_stats"]),
                "fetches": fetch_rows,
                "ingested": _ingestion_summary(summary) if summary else None,
            }
        )

    coverage = (
        build_phase3af_coverage_diagnostics(session, settings=settings)
        if include_coverage and session is not None
        else None
    )

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AF",
        "phase_version": PHASE_3AF_VERSION,
        "mode": "PAPER_ONLY_SPORTS_SCHEDULE_BOOTSTRAP",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "source": ESPN_SOURCE,
        "start_date": first_date.isoformat(),
        "days_ahead": days_ahead,
        "leagues": requested_leagues,
        "soccer_competitions": competitions,
        "schedule_output_dir": str(schedule_output_dir),
        "schedule_paths": [str(path) for path in schedule_paths],
        "legacy_sample_path": str(legacy_sample_path) if legacy_sample_path else None,
        "ingest_requested": ingest,
        "ingestion_summaries": ingestion_summaries,
        "league_results": league_results,
        "coverage_diagnostics": coverage,
        "summary": {
            "leagues_requested": len(requested_leagues),
            "files_written": len(schedule_paths),
            "legacy_sample_written": legacy_sample_path is not None,
            "teams_written": sum(row["teams"] for row in league_results),
            "games_written": sum(row["games"] for row in league_results),
            "team_stats_written": sum(row["team_stats"] for row in league_results),
            "teams_inserted": sum(row.get("teams_inserted", 0) for row in ingestion_summaries),
            "games_inserted": sum(row.get("games_inserted", 0) for row in ingestion_summaries),
            "errors": len(total_errors),
        },
        "errors": total_errors,
        "recommended_next_action": _next_action(ingest=ingest, paths=schedule_paths),
    }


def write_phase3af_report(
    session: Session | None = None,
    *,
    output_dir: Path = Path("reports/phase3af"),
    schedule_output_dir: Path = Path("data/sports_schedules"),
    leagues: str | list[str] | tuple[str, ...] = DEFAULT_LEAGUES,
    start_date: str | date | None = None,
    days_ahead: int = 7,
    ingest: bool = False,
    write_legacy_sample: bool = True,
    timeout_seconds: float = 20.0,
    soccer_competitions: str | list[str] | tuple[str, ...] = DEFAULT_SOCCER_COMPETITIONS,
    include_coverage: bool = True,
    settings: Settings | None = None,
) -> Phase3AFArtifactSet:
    payload = run_sports_schedule_bootstrap(
        session,
        leagues=leagues,
        start_date=start_date,
        days_ahead=days_ahead,
        schedule_output_dir=schedule_output_dir,
        ingest=ingest,
        write_legacy_sample=write_legacy_sample,
        timeout_seconds=timeout_seconds,
        soccer_competitions=soccer_competitions,
        include_coverage=include_coverage,
        settings=settings,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3af_sports_schedule_bootstrap.json"
    markdown_path = output_dir / "phase3af_sports_schedule_bootstrap.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AFArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        schedule_paths=tuple(Path(path) for path in payload["schedule_paths"]),
        legacy_sample_path=Path(payload["legacy_sample_path"])
        if payload["legacy_sample_path"]
        else None,
    )


def build_phase3af_coverage_diagnostics(
    session: Session,
    *,
    settings: Settings | None = None,
    max_schedule_delta_hours: int | None = DEFAULT_MAX_SCHEDULE_DELTA_HOURS,
) -> dict[str, Any]:
    """Describe sports schedule-to-market coverage without mutating links or features."""

    resolved = settings or get_settings()
    session.flush()
    markets = list(session.scalars(select(Market).order_by(Market.ticker)))
    teams = list(
        session.scalars(select(SportsTeam).order_by(SportsTeam.league, SportsTeam.team_key))
    )
    games = list(
        session.scalars(select(SportsGame).order_by(SportsGame.league, SportsGame.game_key))
    )
    links = list(
        session.scalars(
            select(SportsMarketLink).order_by(SportsMarketLink.ticker, desc(SportsMarketLink.id))
        )
    )
    snapshots = list(
        session.scalars(
            select(MarketSnapshot).order_by(
                MarketSnapshot.ticker,
                desc(MarketSnapshot.captured_at),
                desc(MarketSnapshot.id),
            )
        )
    )
    features = list(
        session.scalars(
            select(SportsFeature).order_by(SportsFeature.ticker, desc(SportsFeature.id))
        )
    )
    verified_games = [game for game in games if _phase3af_game_is_verified(game)]
    verified_games_by_league: dict[str, list[SportsGame]] = {}
    for game in verified_games:
        verified_games_by_league.setdefault(game.league, []).append(game)
    team_by_key = {team.team_key: team for team in teams}
    latest_links = _latest_link_by_ticker(links)
    latest_features = _latest_feature_by_ticker(features)
    latest_snapshots = _latest_snapshot_by_ticker(snapshots)
    market_tickers = {market.ticker for market in markets}
    game_keys = {game.game_key for game in games}

    rows: list[dict[str, Any]] = []
    rejection_counts: Counter[str] = Counter()
    exact_matches = 0
    ambiguous_matches = 0
    linked_events_with_features = 0
    forecast_eligible = 0

    for market in markets:
        latest_link = latest_links.get(market.ticker)
        classification = _phase3af_market_classification(market, latest_link)
        if not classification["is_sports"]:
            continue
        row = _phase3af_market_row(
            session,
            market,
            classification=classification,
            verified_games_by_league=verified_games_by_league,
            team_by_key=team_by_key,
            latest_link=latest_link,
            latest_feature=latest_features.get(market.ticker),
            latest_snapshot=latest_snapshots.get(market.ticker),
            min_confidence=resolved.sports_min_link_confidence,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
        rows.append(row)
        if row["status"] == "SPORTS_V1_FORECAST_ELIGIBLE":
            exact_matches += 1
            linked_events_with_features += 1
            forecast_eligible += 1
        elif row["status"] == "VERIFIED_LINK_MISSING_FEATURE":
            exact_matches += 1
            rejection_counts[row["reason_code"]] += 1
        elif row["status"] == "VERIFIED_LINK_MISSING_SNAPSHOT_PRICE":
            exact_matches += 1
            linked_events_with_features += 1
            rejection_counts[row["reason_code"]] += 1
        elif row["status"] == "AMBIGUOUS_VERIFIED_MATCH":
            ambiguous_matches += 1
            rejection_counts[row["reason_code"]] += 1
        else:
            rejection_counts[row["reason_code"]] += 1

    orphaned_legacy_links = [
        link
        for link in links
        if _phase3af_link_provenance(link) != "verified_schedule"
        and (link.ticker not in market_tickers or link.game_key not in game_keys)
    ]
    status_counts = Counter(str(game.status or "unknown").lower() for game in games)
    league_counts = Counter(game.league for game in games)
    coverage_ratio = (
        (forecast_eligible / len(rows)) if rows else None
    )

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AF",
        "mode": "PAPER_ONLY_SPORTS_COVERAGE_DIAGNOSTICS",
        "max_schedule_delta_hours": max_schedule_delta_hours,
        "min_confidence": str(resolved.sports_min_link_confidence),
        "summary": {
            "schedule_events_ingested": len(games),
            "verified_schedule_events": len(verified_games),
            "canonical_teams": len(teams),
            "eligible_kalshi_sports_markets": len(rows),
            "exact_matches": exact_matches,
            "ambiguous_matches": ambiguous_matches,
            "rejected_matches": sum(rejection_counts.values()),
            "orphaned_legacy_links": len(orphaned_legacy_links),
            "linked_events_with_usable_features": linked_events_with_features,
            "sports_v1_forecast_eligible": forecast_eligible,
            "coverage_ratio": round(coverage_ratio, 4) if coverage_ratio is not None else None,
        },
        "game_status_counts": dict(sorted(status_counts.items())),
        "game_league_counts": dict(sorted(league_counts.items())),
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "orphaned_legacy_links": [
            {
                "ticker": link.ticker,
                "league": link.league,
                "game_key": link.game_key,
                "market_type": link.market_type,
                "provenance": _phase3af_link_provenance(link),
            }
            for link in orphaned_legacy_links[:100]
        ],
        "golden_trace": _phase3af_golden_trace(rows),
        "market_rows": rows,
        "recommended_next_action": _phase3af_coverage_next_action(
            rows=rows,
            verified_games=len(verified_games),
            forecast_eligible=forecast_eligible,
            rejection_counts=rejection_counts,
        ),
    }


def _phase3af_market_row(
    _session: Session,
    market: Market,
    *,
    classification: dict[str, Any],
    verified_games_by_league: dict[str, list[SportsGame]],
    team_by_key: dict[str, SportsTeam],
    latest_link: SportsMarketLink | None,
    latest_feature: SportsFeature | None,
    latest_snapshot: MarketSnapshot | None,
    min_confidence: Decimal,
    max_schedule_delta_hours: int | None,
) -> dict[str, Any]:
    snapshot = latest_snapshot
    link_provenance = _phase3af_link_provenance(latest_link) if latest_link else None
    base = {
        "ticker": market.ticker,
        "title": market.title,
        "market_status": market.status,
        "close_time": market.close_time.isoformat() if market.close_time else None,
        "classification": {
            "league": classification.get("league"),
            "market_type": classification.get("market_type"),
            "matched_terms": classification.get("matched_terms"),
        },
        "latest_link_id": latest_link.id if latest_link else None,
        "latest_link_provenance": link_provenance,
        "latest_link_game_key": latest_link.game_key if latest_link else None,
        "latest_feature_id": latest_feature.id if latest_feature else None,
        "latest_snapshot_id": snapshot.id if snapshot else None,
        "verified_candidate_count": 0,
        "top_candidate_game_key": None,
        "top_candidate_confidence": None,
        "reason_code": None,
        "status": None,
        "next_action": None,
    }

    if classification.get("league") == UNKNOWN:
        return {
            **base,
            "status": "REJECTED",
            "reason_code": "unknown_sports_league",
            "next_action": "Improve sports classifier aliases for this market text.",
        }

    candidate_games = [
        game
        for game in verified_games_by_league.get(str(classification.get("league")), [])
        if game.league == classification.get("league")
        and _schedule_window_allowed(
            market,
            game,
            max_schedule_delta_hours=max_schedule_delta_hours,
        )
    ]
    base["verified_candidate_count"] = len(candidate_games)
    if not candidate_games:
        return {
            **base,
            "status": "REJECTED",
            "reason_code": "no_verified_schedule_game_for_league_time",
            "next_action": (
                "Run phase3af-sports-schedule-bootstrap with the needed league/date "
                "or import a verified local fixture."
            ),
        }

    matches = _phase3af_match_preview(
        market,
        classification=classification,
        games=candidate_games,
        team_by_key=team_by_key,
        min_confidence=min_confidence,
    )
    if matches:
        base["top_candidate_game_key"] = matches[0]["game_key"]
        base["top_candidate_confidence"] = matches[0]["confidence"]
    if len(matches) >= 2 and Decimal(matches[0]["confidence"]) - Decimal(
        matches[1]["confidence"]
    ) < AMBIGUITY_MARGIN:
        return {
            **base,
            "status": "AMBIGUOUS_VERIFIED_MATCH",
            "reason_code": "multiple_verified_games_plausible",
            "next_action": "Do not link automatically; add team/event/date specificity.",
        }
    if not matches:
        return {
            **base,
            "status": "REJECTED",
            "reason_code": "no_verified_game_match_above_threshold",
            "next_action": (
                "Check team aliases, market type, event date, and source schedule coverage."
            ),
        }

    if latest_link is None:
        return {
            **base,
            "status": "REJECTED",
            "reason_code": "no_sports_market_link",
            "next_action": "Run phase3ae-verified-sports-connector after schedule ingestion.",
        }
    if link_provenance != "verified_schedule":
        return {
            **base,
            "status": "REJECTED",
            "reason_code": "legacy_or_partial_link_only",
            "next_action": "Run phase3ae-verified-sports-connector to upgrade partial provenance.",
        }
    if latest_link.game_key != matches[0]["game_key"]:
        return {
            **base,
            "status": "REJECTED",
            "reason_code": "verified_link_points_to_different_game",
            "next_action": (
                "Review stale verified link; do not trust until game identity is reconciled."
            ),
        }
    if latest_feature is None or latest_feature.game_key != latest_link.game_key:
        return {
            **base,
            "status": "VERIFIED_LINK_MISSING_FEATURE",
            "reason_code": "verified_link_missing_feature",
            "next_action": (
                "Run build-sports-features or phase3ae-verified-sports-connector "
                "--refresh-features."
            ),
        }
    if snapshot is None or not _snapshot_has_price(snapshot):
        return {
            **base,
            "status": "VERIFIED_LINK_MISSING_SNAPSHOT_PRICE",
            "reason_code": "verified_link_missing_snapshot_price",
            "next_action": (
                "Run collect-once for fresh market snapshots before sports_v1 forecasts."
            ),
        }
    return {
        **base,
        "status": "SPORTS_V1_FORECAST_ELIGIBLE",
        "reason_code": "ready",
        "next_action": "Ready for forecast --model sports_v1.",
    }


def _phase3af_match_preview(
    market: Market,
    *,
    classification: dict[str, Any],
    games: list[SportsGame],
    team_by_key: dict[str, SportsTeam],
    min_confidence: Decimal,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for game in games:
        confidence, reason, matched_terms, market_type = score_sports_market_link(
            market,
            game,
            home_team=team_by_key.get(game.home_team_key),
            away_team=team_by_key.get(game.away_team_key),
            classification=classification,
        )
        if confidence < min_confidence:
            continue
        rows.append(
            {
                "game_key": game.game_key,
                "confidence": str(confidence),
                "reason": reason,
                "matched_terms": ",".join(matched_terms),
                "market_type": market_type,
            }
        )
    return sorted(rows, key=lambda row: (-Decimal(row["confidence"]), row["game_key"]))


def _phase3af_market_classification(
    market: Market,
    latest_link: SportsMarketLink | None,
) -> dict[str, Any]:
    classification = classify_sports_market(market)
    if latest_link is None:
        return classification
    adjusted = dict(classification)
    link_league = str(latest_link.league or UNKNOWN).upper()
    link_market_type = str(latest_link.market_type or UNKNOWN).upper()
    if adjusted.get("league") == UNKNOWN and link_league not in {"", "ALL", UNKNOWN}:
        adjusted["league"] = link_league
    if adjusted.get("market_type") == UNKNOWN and link_market_type != UNKNOWN:
        adjusted["market_type"] = link_market_type
    if not adjusted.get("is_sports"):
        adjusted["is_sports"] = link_league not in {"", "ALL", UNKNOWN}
        adjusted["matched_terms"] = ["existing_sports_link"]
    return adjusted


def _latest_link_by_ticker(links: list[SportsMarketLink]) -> dict[str, SportsMarketLink]:
    latest: dict[str, SportsMarketLink] = {}
    for link in links:
        latest.setdefault(link.ticker, link)
    return latest


def _latest_feature_by_ticker(features: list[SportsFeature]) -> dict[str, SportsFeature]:
    latest: dict[str, SportsFeature] = {}
    for feature in features:
        if feature.ticker:
            latest.setdefault(feature.ticker, feature)
    return latest


def _latest_snapshot_by_ticker(snapshots: list[MarketSnapshot]) -> dict[str, MarketSnapshot]:
    latest: dict[str, MarketSnapshot] = {}
    for snapshot in snapshots:
        latest.setdefault(snapshot.ticker, snapshot)
    return latest


def _snapshot_has_price(snapshot: MarketSnapshot) -> bool:
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    if yes_bid is not None and yes_ask is not None and midpoint(yes_bid, yes_ask) is not None:
        return True
    return to_decimal(snapshot.last_price_dollars) is not None


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


def _phase3af_game_is_verified(game: SportsGame) -> bool:
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


def _phase3af_link_provenance(link: SportsMarketLink | None) -> str | None:
    if link is None:
        return None
    raw = decode_json(link.raw_json)
    source = str(raw.get("source") or "").lower()
    reason = str(link.link_reason or "").lower()
    game_key = str(link.game_key or "").lower()
    if source == "verified_schedule":
        return "verified_schedule"
    if "kalshi-event-derived" in game_key or source == "kalshi_event_derived":
        return "kalshi_event_derived"
    if (
        "market-derived" in game_key
        or source == "market-derived-fallback"
        or "market-derived" in reason
    ):
        return "partial_market_derived"
    return "verified_schedule"


def _phase3af_golden_trace(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ready = [row for row in rows if row["status"] == "SPORTS_V1_FORECAST_ELIGIBLE"]
    if not ready:
        first_blocked = rows[0] if rows else None
        return {
            "status": "MISSING",
            "message": "No complete paper-only sports_v1 trace is available yet.",
            "blocked_example": first_blocked,
        }
    row = ready[0]
    return {
        "status": "READY",
        "provider_or_fixture": "verified sports schedule row",
        "canonical_game": row["latest_link_game_key"],
        "kalshi_market": row["ticker"],
        "validated_link_id": row["latest_link_id"],
        "sports_feature_id": row["latest_feature_id"],
        "snapshot_id": row["latest_snapshot_id"],
        "forecast_eligibility": "sports_v1 ready",
    }


def _phase3af_coverage_next_action(
    *,
    rows: list[dict[str, Any]],
    verified_games: int,
    forecast_eligible: int,
    rejection_counts: Counter[str],
) -> str:
    if not rows:
        return "Collect Kalshi markets and run the sports schedule bootstrap."
    if verified_games == 0:
        return (
            "Ingest verified schedules: kalshi-bot phase3af-sports-schedule-bootstrap "
            "--leagues MLB,WNBA,SOCCER --days-ahead 14 --ingest"
        )
    if forecast_eligible:
        return "Run kalshi-bot forecast --model sports_v1, then review sports forecasts."
    if rejection_counts:
        top_reason, _ = rejection_counts.most_common(1)[0]
        return f"Top sports_v1 blocker is {top_reason}; fix that before more blind learning cycles."
    return "Sports coverage diagnostics are clean."


def _build_league_payload(
    *,
    league: str,
    dates: list[date],
    timeout_seconds: float,
    soccer_competitions: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    teams: dict[str, dict[str, Any]] = {}
    games: dict[str, dict[str, Any]] = {}
    team_stats: dict[tuple[str, str], dict[str, Any]] = {}
    fetch_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    source_specs = _source_specs(league, soccer_competitions=soccer_competitions)
    for current_date in dates:
        for source_spec in source_specs:
            url = _espn_url(source_spec["sport"], source_spec["league_path"], current_date)
            try:
                response = httpx.get(url, timeout=timeout_seconds)
                response.raise_for_status()
                raw_payload = response.json()
            except Exception as exc:  # noqa: BLE001 - diagnostics should continue.
                errors.append(f"{league} {current_date.isoformat()} {url}: {exc}")
                fetch_rows.append(
                    {
                        "date": current_date.isoformat(),
                        "url": url,
                        "status": "ERROR",
                        "events": 0,
                    }
                )
                continue
            converted = _convert_espn_scoreboard(
                raw_payload,
                league=league,
                source_url=url,
                competition_code=source_spec.get("competition_code"),
            )
            for team in converted["teams"]:
                teams[team["team_key"]] = team
            for game in converted["games"]:
                games[game["game_key"]] = game
            for stat in converted["team_stats"]:
                team_stats[(stat["team_key"], stat["as_of"])] = stat
            fetch_rows.append(
                {
                    "date": current_date.isoformat(),
                    "url": url,
                    "status": "OK",
                    "events": converted["events_seen"],
                    "games": len(converted["games"]),
                }
            )
    return (
        {
            "league": league,
            "source": ESPN_SOURCE,
            "source_note": "Fetched by Phase 3AF from ESPN public scoreboard endpoints.",
            "generated_at": utc_now().isoformat(),
            "teams": sorted(teams.values(), key=lambda row: row["team_key"]),
            "games": sorted(games.values(), key=lambda row: row["game_key"]),
            "team_stats": sorted(
                team_stats.values(),
                key=lambda row: (row["team_key"], row["as_of"]),
            ),
            "injuries": [],
            "odds": [],
        },
        fetch_rows,
        errors,
    )


def _source_specs(league: str, *, soccer_competitions: list[str]) -> list[dict[str, str]]:
    if league == "SOCCER":
        return [
            {"sport": "soccer", "league_path": competition, "competition_code": competition}
            for competition in soccer_competitions
        ]
    sport, league_path = ESPN_LEAGUE_PATHS[league]
    return [{"sport": sport, "league_path": league_path}]


def _espn_url(sport: str, league_path: str, current_date: date) -> str:
    stamp = current_date.strftime("%Y%m%d")
    return f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league_path}/scoreboard?dates={stamp}"


def _convert_espn_scoreboard(
    payload: dict[str, Any],
    *,
    league: str,
    source_url: str,
    competition_code: str | None,
) -> dict[str, Any]:
    teams: dict[str, dict[str, Any]] = {}
    games: list[dict[str, Any]] = []
    team_stats: list[dict[str, Any]] = []
    events = [event for event in payload.get("events", []) if isinstance(event, dict)]
    for event in events:
        competition = _first_dict(event.get("competitions"))
        competitors = [
            item for item in competition.get("competitors", []) if isinstance(item, dict)
        ]
        home = _competitor_by_side(competitors, "home")
        away = _competitor_by_side(competitors, "away")
        if home is None or away is None:
            continue
        home_team = _team_payload(home, league=league, source_url=source_url)
        away_team = _team_payload(away, league=league, source_url=source_url)
        teams[home_team["team_key"]] = home_team
        teams[away_team["team_key"]] = away_team
        scheduled_at = str(event.get("date") or competition.get("date") or "").strip()
        if not scheduled_at:
            continue
        game_key = f"{league}:espn:{competition_code or league.lower()}:{event.get('id')}"
        games.append(
            {
                "game_key": game_key,
                "scheduled_at": scheduled_at,
                "season": str(_season_year(event, payload) or ""),
                "status": _status(event, competition),
                "home_team_key": home_team["team_key"],
                "away_team_key": away_team["team_key"],
                "home_score": _score(home),
                "away_score": _score(away),
                "venue": _venue(competition),
                "neutral_site": bool(competition.get("neutralSite") or False),
                "source": ESPN_SOURCE,
                "source_url": source_url,
                "source_competition": competition_code,
                "espn_event_id": str(event.get("id") or ""),
                "event_name": event.get("name") or event.get("shortName"),
            }
        )
        for competitor, team in ((home, home_team), (away, away_team)):
            record = _record_stat(competitor, team_key=team["team_key"], as_of=scheduled_at)
            if record is not None:
                team_stats.append(record)
    return {
        "events_seen": len(events),
        "teams": list(teams.values()),
        "games": games,
        "team_stats": team_stats,
    }


def _team_payload(
    competitor: dict[str, Any],
    *,
    league: str,
    source_url: str,
) -> dict[str, Any]:
    team = competitor.get("team") if isinstance(competitor.get("team"), dict) else {}
    key = (
        team.get("abbreviation")
        or team.get("shortDisplayName")
        or team.get("displayName")
        or team.get("name")
        or team.get("id")
    )
    payload = {
        "team_key": str(key or "unknown"),
        "team_name": str(team.get("displayName") or team.get("name") or key or "Unknown"),
        "abbreviation": _str_or_none(team.get("abbreviation")),
        "city": _str_or_none(team.get("location")),
        "short_name": _str_or_none(team.get("shortDisplayName")),
        "nickname": _str_or_none(team.get("name")),
        "venue": _str_or_none(team.get("venue", {}).get("fullName"))
        if isinstance(team.get("venue"), dict)
        else None,
        "source": ESPN_SOURCE,
        "source_url": source_url,
        "source_league": league,
        "espn_team_id": str(team.get("id") or ""),
    }
    payload["aliases"] = sports_team_aliases_from_payload(payload)
    return payload


def _record_stat(
    competitor: dict[str, Any],
    *,
    team_key: str,
    as_of: str,
) -> dict[str, Any] | None:
    records = [item for item in competitor.get("records", []) if isinstance(item, dict)]
    summary = ""
    for record in records:
        if record.get("type") in {"total", "overall"} or not summary:
            summary = str(record.get("summary") or "")
    wins, losses = _parse_record(summary)
    if wins is None and losses is None:
        return None
    return {
        "team_key": team_key,
        "as_of": as_of,
        "wins": wins,
        "losses": losses,
        "games_played": (wins or 0) + (losses or 0),
        "source": ESPN_SOURCE,
        "record_summary": summary,
    }


def _parse_record(summary: str) -> tuple[int | None, int | None]:
    parts = summary.replace(" ", "").split("-")
    if len(parts) < 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


def _competitor_by_side(
    competitors: list[dict[str, Any]],
    side: str,
) -> dict[str, Any] | None:
    for competitor in competitors:
        if str(competitor.get("homeAway") or "").lower() == side:
            return competitor
    return None


def _first_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _season_year(event: dict[str, Any], payload: dict[str, Any]) -> Any:
    season = event.get("season") if isinstance(event.get("season"), dict) else {}
    if season.get("year"):
        return season.get("year")
    payload_season = payload.get("season") if isinstance(payload.get("season"), dict) else {}
    return payload_season.get("year")


def _status(event: dict[str, Any], competition: dict[str, Any]) -> str:
    status = competition.get("status") if isinstance(competition.get("status"), dict) else {}
    if not status:
        status = event.get("status") if isinstance(event.get("status"), dict) else {}
    status_type = status.get("type") if isinstance(status.get("type"), dict) else {}
    name = str(status_type.get("name") or status_type.get("description") or "").lower()
    state = str(status_type.get("state") or "").lower()
    detail = f"{state} {name}".strip()
    if status_type.get("completed") or state == "post":
        return "final"
    if "postponed" in detail:
        return "postponed"
    if "delayed" in detail:
        return "delayed"
    if "cancel" in detail:
        return "cancelled"
    if "rescheduled" in detail:
        return "rescheduled"
    if state == "pre":
        return "scheduled"
    return normalize_sports_status(name or state or "scheduled")


def _score(competitor: dict[str, Any]) -> int | None:
    value = competitor.get("score")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _venue(competition: dict[str, Any]) -> str | None:
    venue = competition.get("venue") if isinstance(competition.get("venue"), dict) else {}
    return _str_or_none(venue.get("fullName") or venue.get("name"))


def _schedule_filename(league: str, start_date: date, days_ahead: int) -> str:
    return f"sports_verified_{league.lower()}_{start_date.strftime('%Y%m%d')}_{days_ahead}d.json"


def _parse_leagues(value: str | list[str] | tuple[str, ...]) -> list[str]:
    leagues = _parse_csv(value)
    if not leagues:
        raise ValueError("At least one league is required.")
    normalized: list[str] = []
    for league in leagues:
        normalized_league = normalize_league(league)
        if normalized_league == "ALL":
            normalized.extend(item for item in DEFAULT_LEAGUES if item not in normalized)
            continue
        if normalized_league not in ESPN_LEAGUE_PATHS and normalized_league != "SOCCER":
            raise ValueError(
                "Phase 3AF currently supports ESPN schedule bootstrap for "
                "MLB, NBA, WNBA, NFL, NHL, and SOCCER."
            )
        if normalized_league not in normalized:
            normalized.append(normalized_league)
    return normalized


def _parse_csv(value: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = []
        for item in value:
            parts.extend(str(item).split(","))
    return [part.strip() for part in parts if part and part.strip()]


def _parse_date(value: str | date | None) -> date:
    if value is None:
        return utc_now().date()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("start date must be YYYY-MM-DD or YYYYMMDD.")


def _ingestion_summary(summary: SportsIngestionSummary | None) -> dict[str, Any]:
    if summary is None:
        return {}
    return {
        "league": summary.league,
        "source": summary.source,
        "teams_seen": summary.teams_seen,
        "teams_inserted": summary.teams_inserted,
        "games_seen": summary.games_seen,
        "games_inserted": summary.games_inserted,
        "team_stats_inserted": summary.team_stats_inserted,
        "injuries_inserted": summary.injuries_inserted,
        "odds_inserted": summary.odds_inserted,
        "errors": summary.errors,
    }


def _next_action(*, ingest: bool, paths: list[Path]) -> str:
    if not paths:
        return "No schedule files were written. Check network access and source settings."
    if not ingest:
        first = paths[0]
        return (
            "Ingest the written verified schedule file, then rerun Phase 3AE: "
            f"kalshi-bot ingest-sports --league MLB --input-file {first} && "
            "kalshi-bot phase3ae-verified-sports-connector --output-dir reports/phase3ae"
        )
    return (
        "Verified schedule rows were ingested. Rerun sports link upgrade: "
        "kalshi-bot phase3ae-verified-sports-connector --output-dir reports/phase3ae"
    )


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AF Sports Schedule Bootstrap",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Source: {payload['source']}",
        f"- Start date: {payload['start_date']}",
        f"- Days ahead: {payload['days_ahead']}",
        f"- Ingest requested: {payload['ingest_requested']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Schedule Files",
            "",
        ]
    )
    for path in payload["schedule_paths"]:
        lines.append(f"- `{path}`")
    if payload["legacy_sample_path"]:
        lines.append(f"- Legacy sample copy: `{payload['legacy_sample_path']}`")
    lines.extend(
        [
            "",
            "## League Results",
            "",
            "| League | Teams | Games | Team stats | File |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in payload["league_results"]:
        lines.append(
            f"| {row['league']} | {row['teams']} | {row['games']} | "
            f"{row['team_stats']} | `{row['output_path']}` |"
        )
    lines.extend(["", "## Errors", ""])
    if payload["errors"]:
        for error in payload["errors"][:25]:
            lines.append(f"- {error}")
    else:
        lines.append("- none")
    coverage = payload.get("coverage_diagnostics")
    if coverage:
        coverage_summary = coverage["summary"]
        lines.extend(
            [
                "",
                "## Coverage Diagnostics",
                "",
            ]
        )
        for key, value in coverage_summary.items():
            lines.append(f"- {key}: {value}")
        lines.extend(
            [
                "",
                "### Rejection Reasons",
                "",
            ]
        )
        if coverage["rejection_counts"]:
            for reason, count in coverage["rejection_counts"].items():
                lines.append(f"- {reason}: {count}")
        else:
            lines.append("- none")
        lines.extend(
            [
                "",
                "### Golden Trace",
                "",
                f"- Status: {coverage['golden_trace']['status']}",
                f"- Message: {coverage['golden_trace'].get('message', 'sports_v1 ready')}",
            ]
        )
        if coverage["golden_trace"]["status"] == "READY":
            trace = coverage["golden_trace"]
            lines.extend(
                [
                    f"- Canonical game: `{trace['canonical_game']}`",
                    f"- Kalshi market: `{trace['kalshi_market']}`",
                    f"- Link id: {trace['validated_link_id']}",
                    f"- Feature id: {trace['sports_feature_id']}",
                    f"- Snapshot id: {trace['snapshot_id']}",
                    f"- Forecast eligibility: {trace['forecast_eligibility']}",
                ]
            )
        lines.extend(
            [
                "",
                "### Market Coverage Rows",
                "",
                "| Ticker | Status | League | Link provenance | Game | Reason | Next action |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for row in coverage["market_rows"][:50]:
            lines.append(
                f"| `{row['ticker']}` | {row['status']} | "
                f"{row['classification']['league']} | "
                f"{row['latest_link_provenance'] or ''} | "
                f"`{row['latest_link_game_key'] or row['top_candidate_game_key'] or ''}` | "
                f"{row['reason_code']} | {_md(row['next_action'])} |"
            )
        if not coverage["market_rows"]:
            lines.append("| none |  |  |  |  |  |  |")
        lines.extend(
            [
                "",
                "### Coverage Next Action",
                "",
                coverage["recommended_next_action"],
            ]
        )
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Safety",
            "",
            "- Paper-only schedule/link data repair.",
            "- No demo orders.",
            "- No live orders.",
            "- No exchange writes.",
            "",
        ]
    )
    return "\n".join(lines)


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
