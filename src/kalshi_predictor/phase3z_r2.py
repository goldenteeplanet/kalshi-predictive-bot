from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

import kalshi_predictor
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import detect_backend, redact_database_url
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market, MarketLeg, SportsGame, SportsMarketLink
from kalshi_predictor.market_legs import CATEGORY_SPORTS
from kalshi_predictor.phase3z import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE3Z_R2_VERSION = "phase3z_r2_sports_provenance_repair_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3z_r2")
PLACEHOLDER_RE = re.compile(
    r"(?i)(?:^|[:_\-\s])(rd(?:16|32)-w\d+|rd(?:16|32)|round of 16|round of 32|placeholder)"
)
SYNTHETIC_TEAM_RE = re.compile(r"(?i)(?:kxmv|crosscategory|multigame|market-derived|-yes$|-no$)")
MVE_FAMILY_RE = re.compile(r"(?i)^KXMVE(?:CROSSCATEGORY|SPORTSMULTIGAMEEXTENDED)")


@dataclass(frozen=True)
class Phase3ZR2ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def build_phase3z_r2_sports_provenance_repair(
    session: Session,
    *,
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    sample_limit: int = 25,
    max_rows: int | None = 1000,
    ticker_prefix: str | None = None,
) -> dict[str, Any]:
    session.flush()
    report_inputs = _load_report_inputs(reports_dir)
    coverage_sports = _sports_coverage_row_from_inputs(report_inputs)
    row_limit = _normalized_max_rows(max_rows)
    normalized_prefix = _normalized_ticker_prefix(ticker_prefix)
    provenance_counts = _sports_link_provenance_counts(
        session,
        ticker_prefix=normalized_prefix,
    )
    total_sports_link_rows = _count_sports_links(session, ticker_prefix=normalized_prefix)
    total_sports_parsed_markets = _total_sports_parsed_markets(
        session,
        coverage_sports=coverage_sports,
        ticker_prefix=normalized_prefix,
    )
    raw_unresolved_partial_tickers = _raw_unresolved_partial_sports_tickers(
        session,
        ticker_prefix=normalized_prefix,
    )
    excluded_partial_tickers = _sports_partial_tickers_excluded_from_repair(
        session,
        raw_unresolved_partial_tickers,
    )
    unresolved_partial_tickers = raw_unresolved_partial_tickers - excluded_partial_tickers
    unlinked_count = _unlinked_sports_market_count(
        session,
        coverage_sports=coverage_sports,
        ticker_prefix=normalized_prefix,
    )
    partial_row_tickers = _bounded_sorted_tickers(unresolved_partial_tickers, row_limit)
    remaining_row_slots = (
        None if row_limit is None else max(row_limit - len(partial_row_tickers), 0)
    )
    unlinked_tickers = (
        _unlinked_sports_tickers(
            session,
            ticker_prefix=normalized_prefix,
            limit=remaining_row_slots,
        )
        if unlinked_count and remaining_row_slots != 0
        else set()
    )
    row_tickers = partial_row_tickers | unlinked_tickers
    sports_links = _sports_links_for_tickers(session, row_tickers)
    sports_legs = _sports_legs_for_tickers(session, row_tickers)
    links_by_ticker = _links_by_ticker(sports_links)
    legs_by_ticker = _legs_by_ticker(sports_legs)
    markets = _markets_by_ticker(session, row_tickers)
    games = _sports_games_for_links(session, sports_links)
    placeholder_examples = _placeholder_example_tickers(report_inputs["placeholder_watch"])
    rows = _degraded_rows(
        markets=markets,
        legs_by_ticker=legs_by_ticker,
        links_by_ticker=links_by_ticker,
        games=games,
        unresolved_partial_tickers=partial_row_tickers,
        unlinked_tickers=unlinked_tickers,
        placeholder_examples=placeholder_examples,
        sample_limit=sample_limit,
    )
    row_scan = _row_scan(
        max_rows=row_limit,
        ticker_prefix=normalized_prefix,
        unresolved_partial_tickers=unresolved_partial_tickers,
        unlinked_count=unlinked_count,
        rows=rows,
    )
    groups = _group_rows(rows, sample_limit=sample_limit)
    summary = _summary(
        report_inputs=report_inputs,
        total_sports_link_rows=total_sports_link_rows,
        total_sports_parsed_markets=total_sports_parsed_markets,
        rows=rows,
        provenance_counts=provenance_counts,
        raw_unresolved_partial_tickers=raw_unresolved_partial_tickers,
        excluded_partial_tickers=excluded_partial_tickers,
        unresolved_partial_tickers=unresolved_partial_tickers,
        unlinked_count=unlinked_count,
        row_scan=row_scan,
    )
    count_reconciliation = _count_reconciliation(
        report_inputs=report_inputs,
        summary=summary,
    )
    phase3ae_gate = _phase3ae_gate(summary)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3Z-R2/3AH",
        "phase_version": PHASE3Z_R2_VERSION,
        "mode": "PAPER_ONLY_READ_ONLY_SPORTS_PROVENANCE_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "runtime_identity": _lightweight_runtime_identity(session, settings=settings),
        "input_reports": _input_report_paths(reports_dir),
        "source_availability": {
            key: value is not None for key, value in report_inputs.items()
        },
        "row_scan": row_scan,
        "summary": summary,
        "count_reconciliation": count_reconciliation,
        "phase3ae_gate": phase3ae_gate,
        "grouped_degraded_links": groups,
        "degraded_rows": rows,
        "next_commands": _next_commands(phase3ae_gate),
        "recommended_next_action": _recommended_next_action(phase3ae_gate, summary),
    }


def write_phase3z_r2_sports_provenance_repair_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    sample_limit: int = 25,
    max_rows: int | None = 1000,
    ticker_prefix: str | None = None,
) -> Phase3ZR2ArtifactSet:
    payload = build_phase3z_r2_sports_provenance_repair(
        session,
        reports_dir=reports_dir,
        settings=settings,
        sample_limit=sample_limit,
        max_rows=max_rows,
        ticker_prefix=ticker_prefix,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3z_r2_sports_provenance_repair.json"
    markdown_path = output_dir / "phase3z_r2_sports_provenance_repair.md"
    rows_path = output_dir / "sports_provenance_repair_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["degraded_rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3ZR2ArtifactSet(output_dir, json_path, markdown_path, rows_path)


def _degraded_rows(
    *,
    markets: dict[str, Market],
    legs_by_ticker: dict[str, list[MarketLeg]],
    links_by_ticker: dict[str, list[SportsMarketLink]],
    games: dict[tuple[str, str], SportsGame],
    unresolved_partial_tickers: set[str],
    unlinked_tickers: set[str],
    placeholder_examples: set[str],
    sample_limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ticker in sorted(unresolved_partial_tickers):
        links = [
            link
            for link in links_by_ticker.get(ticker, [])
            if _link_provenance(link) == "partial_market_derived"
        ]
        link = links[0] if links else None
        rows.append(
            _row(
                source_kind="PARTIAL_LEGACY_IDENTIFIER",
                reason_code="LEGACY_IDENTIFIER",
                ticker=ticker,
                market=markets.get(ticker),
                legs=legs_by_ticker.get(ticker, []),
                links=links,
                primary_link=link,
                game=_game_for_link(link, games),
                placeholder_examples=placeholder_examples,
                sample_limit=sample_limit,
            )
        )
    for ticker in sorted(unlinked_tickers):
        rows.append(
            _row(
                source_kind="UNLINKED_PARSED_MARKET",
                reason_code="UNLINKED_PARSED_MARKET",
                ticker=ticker,
                market=markets.get(ticker),
                legs=legs_by_ticker.get(ticker, []),
                links=[],
                primary_link=None,
                game=None,
                placeholder_examples=placeholder_examples,
                sample_limit=sample_limit,
            )
        )
    return sorted(rows, key=lambda row: (row["safe_to_repair"], row["reason_code"], row["ticker"]))


def _row(
    *,
    source_kind: str,
    reason_code: str,
    ticker: str,
    market: Market | None,
    legs: list[MarketLeg],
    links: list[SportsMarketLink],
    primary_link: SportsMarketLink | None,
    game: SportsGame | None,
    placeholder_examples: set[str],
    sample_limit: int,
) -> dict[str, Any]:
    market_type = _clean_market_type(primary_link, legs)
    leg_market_types = sorted({leg.market_type for leg in legs if leg.market_type})
    league = primary_link.league if primary_link else _infer_league(legs)
    placeholder_involved = ticker in placeholder_examples or _has_placeholder(
        primary_link,
        game,
        market,
        legs,
    )
    schedule_evidence = _schedule_evidence(primary_link, game)
    clean_team_identity = _clean_team_identity(game, schedule_evidence, placeholder_involved)
    clean_time = game is not None and game.scheduled_at is not None
    player_prop = market_type == "PLAYER_PROP" or "PLAYER_PROP" in leg_market_types
    multi_leg = len(legs) > 1 or bool(MVE_FAMILY_RE.search(ticker))
    cross_category = _is_cross_category_market(ticker, market)
    clean_market_type = (
        bool(market_type)
        and market_type != "UNKNOWN"
        and len(leg_market_types) <= 1
        and not multi_leg
        and not cross_category
    )
    blockers = _blockers(
        source_kind=source_kind,
        schedule_evidence=schedule_evidence,
        placeholder_involved=placeholder_involved,
        clean_team_identity=clean_team_identity,
        clean_time=clean_time,
        clean_market_type=clean_market_type,
        player_prop=player_prop,
        multi_leg=multi_leg,
        cross_category=cross_category,
    )
    safe_to_repair = not blockers
    return {
        "ticker": ticker,
        "ticker_family": _ticker_family(ticker),
        "title": market.title if market else None,
        "close_time": market.close_time.isoformat() if market and market.close_time else None,
        "source_kind": source_kind,
        "reason_code": reason_code,
        "league": league,
        "market_type": market_type or "UNKNOWN",
        "leg_market_types": leg_market_types,
        "component_count": len(legs),
        "link_row_count": len(links),
        "link_row_ids": [link.id for link in links[:sample_limit]],
        "game_key": primary_link.game_key if primary_link else None,
        "game_status": game.status if game else None,
        "scheduled_at": game.scheduled_at.isoformat() if game and game.scheduled_at else None,
        "home_team_key": game.home_team_key if game else None,
        "away_team_key": game.away_team_key if game else None,
        "placeholder_involved": placeholder_involved,
        "available_schedule_evidence": schedule_evidence,
        "clean_team_identity": clean_team_identity,
        "clean_time": clean_time,
        "clean_market_type": clean_market_type,
        "player_prop_requires_roster": player_prop,
        "unsupported_multi_leg": multi_leg,
        "cross_category": cross_category,
        "safe_to_repair": safe_to_repair,
        "phase3ae_upgrade_allowed": safe_to_repair,
        "blocked_reasons": blockers,
        "example_leg_text": [leg.raw_text for leg in legs[: min(sample_limit, 5)]],
    }


def _blockers(
    *,
    source_kind: str,
    schedule_evidence: str,
    placeholder_involved: bool,
    clean_team_identity: bool,
    clean_time: bool,
    clean_market_type: bool,
    player_prop: bool,
    multi_leg: bool,
    cross_category: bool,
) -> list[str]:
    blockers: list[str] = []
    if source_kind == "PARTIAL_LEGACY_IDENTIFIER":
        blockers.append("PARTIAL_LEGACY_IDENTIFIER")
    if source_kind == "UNLINKED_PARSED_MARKET":
        blockers.append("UNLINKED_PARSED_MARKET")
    if not schedule_evidence.startswith("verified_schedule"):
        blockers.append("NO_VERIFIED_SCHEDULE_EVIDENCE")
    if placeholder_involved:
        blockers.append("PLACEHOLDER_TEAM")
    if not clean_team_identity:
        blockers.append("NO_CLEAN_TEAM_IDENTITY")
    if not clean_time:
        blockers.append("NO_CLEAN_GAME_TIME")
    if not clean_market_type:
        blockers.append("MARKET_TYPE_NOT_CLEAN")
    if player_prop:
        blockers.append("PLAYER_PROP_REQUIRES_ROSTER_EVIDENCE")
    if multi_leg:
        blockers.append("UNSUPPORTED_MULTI_LEG")
    if cross_category:
        blockers.append("CROSS_CATEGORY_COMPONENTS")
    return _dedupe(blockers)


def _group_rows(rows: list[dict[str, Any]], *, sample_limit: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (
            row["reason_code"],
            row["ticker_family"],
            row["market_type"],
            row["league"],
            row["placeholder_involved"],
            row["available_schedule_evidence"],
            row["safe_to_repair"],
        )
        group = grouped.setdefault(
            key,
            {
                "reason_code": row["reason_code"],
                "ticker_family": row["ticker_family"],
                "market_type": row["market_type"],
                "league": row["league"],
                "placeholder_involved": row["placeholder_involved"],
                "available_schedule_evidence": row["available_schedule_evidence"],
                "safe_to_repair": row["safe_to_repair"],
                "count": 0,
                "blocked_reasons": [],
                "example_tickers": [],
            },
        )
        group["count"] += 1
        group["blocked_reasons"] = sorted(
            set(group["blocked_reasons"]) | set(row["blocked_reasons"])
        )
        if len(group["example_tickers"]) < sample_limit:
            group["example_tickers"].append(row["ticker"])
    return sorted(
        grouped.values(),
        key=lambda group: (
            group["safe_to_repair"],
            -group["count"],
            group["reason_code"],
            group["ticker_family"],
        ),
    )


def _summary(
    *,
    report_inputs: dict[str, dict[str, Any] | None],
    total_sports_link_rows: int,
    total_sports_parsed_markets: int,
    rows: list[dict[str, Any]],
    provenance_counts: Counter[str],
    raw_unresolved_partial_tickers: set[str],
    excluded_partial_tickers: set[str],
    unresolved_partial_tickers: set[str],
    unlinked_count: int,
    row_scan: dict[str, Any],
) -> dict[str, Any]:
    coverage_sports = _sports_coverage_row_from_inputs(report_inputs)
    placeholder_summary = _summary_payload(report_inputs["placeholder_watch"])
    safe_rows = [row for row in rows if row["safe_to_repair"]]
    blocked_rows = [row for row in rows if not row["safe_to_repair"]]
    placeholders = [row for row in rows if row["placeholder_involved"]]
    candidate_count = int(row_scan["candidate_degraded_rows"] or 0)
    upstream_placeholder_rows = int(
        placeholder_summary.get("phase3ae_blocked_placeholder_rows")
        or placeholder_summary.get("still_placeholder_rows")
        or 0
    )
    return {
        "total_sports_parsed_markets": total_sports_parsed_markets,
        "total_sports_link_rows": total_sports_link_rows,
        "verified_schedule_markets": int(coverage_sports.get("verified_schedule_markets") or 0),
        "verified_schedule_link_rows": int(
            coverage_sports.get("verified_schedule_link_rows")
            or provenance_counts["verified_schedule"]
        ),
        "kalshi_event_derived_markets": int(
            coverage_sports.get("derived_usable_markets") or 0
        ),
        "kalshi_event_derived_link_rows": provenance_counts["kalshi_event_derived"],
        "raw_partial_legacy_markets": len(raw_unresolved_partial_tickers),
        "excluded_composite_partial_markets": len(excluded_partial_tickers),
        "partial_legacy_markets": len(unresolved_partial_tickers),
        "partial_legacy_link_rows": provenance_counts["partial_market_derived"],
        "unlinked_parsed_markets": unlinked_count,
        "placeholder_blocked_rows": int(
            upstream_placeholder_rows if candidate_count else len(placeholders)
        ),
        "placeholder_involved_degraded_rows": len(placeholders),
        "candidate_degraded_rows": row_scan["candidate_degraded_rows"],
        "rows_reviewed": len(rows),
        "row_scan_complete": row_scan["complete"],
        "row_scan_truncated": row_scan["truncated"],
        "row_scan_max_rows": row_scan["max_rows"],
        "rows_safe_to_repair": len(safe_rows),
        "rows_blocked": len(blocked_rows),
        "safe_to_apply_rows": len(safe_rows),
        "auto_upgrades_created": 0,
        "phase3ae_gate_status": (
            "READY_FOR_PHASE3AE_SAFE_ROWS" if safe_rows else "HOLD_SPORTS_PROVENANCE_UPGRADES"
        ),
    }


def _count_reconciliation(
    *,
    report_inputs: dict[str, dict[str, Any] | None],
    summary: dict[str, Any],
) -> dict[str, Any]:
    coverage_sports = _sports_coverage_row_from_inputs(report_inputs)
    placeholder_summary = _summary_payload(report_inputs["placeholder_watch"])
    phase3az_partial = _phase3az_partial_count(report_inputs["phase3az"])
    return {
        "definitions": [
            {
                "label": "sports_partial_provenance",
                "unit": "distinct unresolved partial markets",
                "source": "Phase 3AZ/orchestrator",
            },
            {
                "label": "partial_markets",
                "unit": "distinct sports tickers with unresolved market-derived links",
                "source": "market-coverage-doctor",
            },
            {
                "label": "partial_link_rows",
                "unit": "raw SportsMarketLink rows with market-derived fallback provenance",
                "source": "market-coverage-doctor / sports_market_links",
            },
            {
                "label": "phase3ah_watch_partial",
                "unit": "watch input summary value; may be older than coverage rows",
                "source": "phase3ah_sports_placeholder_watch",
            },
        ],
        "values": {
            "phase3az_sports_partial_provenance": phase3az_partial,
            "coverage_partial_markets": int(coverage_sports.get("partial_markets") or 0),
            "coverage_partial_link_rows": int(coverage_sports.get("partial_link_rows") or 0),
            "db_partial_legacy_markets": summary["partial_legacy_markets"],
            "db_partial_legacy_link_rows": summary["partial_legacy_link_rows"],
            "phase3ah_watch_sports_partial_links_without_upgrade": placeholder_summary.get(
                "sports_partial_links_without_upgrade"
            ),
        },
        "consistent_market_count": phase3az_partial in {None, summary["partial_legacy_markets"]}
        and int(coverage_sports.get("partial_markets") or 0) == summary["partial_legacy_markets"],
        "explanation": (
            "Phase 3AZ and coverage partial_markets are distinct ticker counts. "
            "Coverage partial_link_rows and Phase 3AH watch values are link-row/input "
            "counts, so they can be larger or stale relative to distinct markets."
        ),
    }


def _phase3ae_gate(summary: dict[str, Any]) -> dict[str, Any]:
    safe_rows = int(summary.get("rows_safe_to_repair") or 0)
    placeholder_rows = int(summary.get("placeholder_blocked_rows") or 0)
    truncated = bool(summary.get("row_scan_truncated"))
    if safe_rows:
        status = "READY_FOR_PHASE3AE_SAFE_ROWS"
        next_action = (
            "Review the safe rows manually, then run Phase 3AE only for clean team + time + "
            "market-type candidates."
        )
    elif truncated:
        status = "HOLD_BOUNDED_SCAN_INCOMPLETE"
        next_action = (
            "No safe rows were found in the bounded scan. Rerun with a higher --max-rows "
            "or a focused --ticker-prefix before treating this as complete."
        )
    elif placeholder_rows:
        status = "HOLD_PLACEHOLDER_UPGRADES"
        next_action = (
            "Keep Phase 3AE blocked for placeholder rows and legacy partial sports links."
        )
    else:
        status = "NO_SAFE_SPORTS_REPAIR_ROWS"
        next_action = "Ingest verified schedules/rosters, then rerun this diagnostic."
    return {
        "status": status,
        "phase3ae_can_run_from_this_report": safe_rows > 0,
        "clean_team_time_market_type_gate_required": True,
        "placeholder_rows_block_phase3ae": placeholder_rows > 0,
        "auto_upgrade_allowed": False,
        "auto_upgrades_created": 0,
        "next_action": next_action,
    }


def _next_commands(gate: dict[str, Any]) -> list[str]:
    commands = [
        "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
        "kalshi-bot phase3ah-sports-placeholder-watch --output-dir reports/phase3ah_sports",
        (
            "kalshi-bot phase3z-r2-sports-provenance-repair "
            "--output-dir reports/phase3z_r2 --max-rows 1000"
        ),
    ]
    if gate["phase3ae_can_run_from_this_report"]:
        commands.append(
            "kalshi-bot phase3ae-verified-sports-connector --output-dir reports/phase3ae"
        )
    commands.extend(
        [
            "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports",
            (
                "kalshi-bot phase-orchestrator --analyze --output reports/phase_orchestrator.md "
                "--json-output reports/phase_orchestrator.json --next-prompt prompts/next_phase.md"
            ),
        ]
    )
    return commands


def _recommended_next_action(gate: dict[str, Any], summary: dict[str, Any]) -> str:
    if gate["phase3ae_can_run_from_this_report"]:
        return gate["next_action"]
    if (
        summary.get("excluded_composite_partial_markets")
        and not summary["partial_legacy_markets"]
        and not summary["unlinked_parsed_markets"]
    ):
        return (
            "Phase 3AE remains blocked because there are no clean sports repair rows; "
            "the remaining partial legacy tickers are cross-category/unsupported composites."
        )
    if summary["partial_legacy_markets"] or summary["unlinked_parsed_markets"]:
        return (
            "Do not run Phase 3AE yet. Backfill/verify sports schedules and roster evidence, "
            "then rerun this report to find clean rows."
        )
    return gate["next_action"]


def _load_report_inputs(reports_dir: Path) -> dict[str, dict[str, Any] | None]:
    paths = _input_report_paths(reports_dir)
    return {key: _load_json(path) for key, path in paths.items()}


def _input_report_paths(reports_dir: Path) -> dict[str, Path]:
    return {
        "phase3az": reports_dir / "phase3az" / "phase3az_gap_analysis.json",
        "market_coverage": reports_dir / "market_coverage" / "market_coverage_doctor.json",
        "coverage_rows": reports_dir / "market_coverage" / "coverage_rows.json",
        "link_coverage": reports_dir / "market_coverage" / "link_coverage.json",
        "placeholder_watch": reports_dir
        / "phase3ah_sports"
        / "phase3ah_sports_placeholder_watch.json",
        "orchestrator": reports_dir / "phase_orchestrator.json",
    }


def _load_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, (dict, list)) else None


def _lightweight_runtime_identity(
    session: Session,
    *,
    settings: Settings | None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    bind = session.get_bind()
    db_url = str(bind.url) if bind is not None else ""
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3Z-R2",
        "phase_version": PHASE3Z_R2_VERSION,
        "identity_mode": "LIGHTWEIGHT_NO_DB_HEALTH_SCAN",
        "current_working_directory": str(Path.cwd().resolve()),
        "python_executable": str(Path(sys.executable).resolve()),
        "package_path": str(Path(kalshi_predictor.__file__).resolve()),
        "database_backend": detect_backend(resolved, db_url=db_url),
        "database_url": redact_database_url(db_url),
        "database_location": describe_db_location(db_url),
    }


def _normalized_max_rows(max_rows: int | None) -> int | None:
    if max_rows is None:
        return None
    return max(max_rows, 0)


def _normalized_ticker_prefix(ticker_prefix: str | None) -> str | None:
    normalized = (ticker_prefix or "").strip().upper()
    return normalized or None


def _ticker_prefix_filters(column: Any, ticker_prefix: str | None) -> list[Any]:
    if not ticker_prefix:
        return []
    return [column.like(f"{ticker_prefix}%")]


def _partial_link_condition() -> Any:
    return or_(
        func.lower(SportsMarketLink.game_key).like("%market-derived%"),
        func.lower(SportsMarketLink.link_reason).like("%market-derived%"),
    )


def _verified_link_condition() -> Any:
    return or_(
        func.lower(SportsMarketLink.link_reason).like("%verified schedule%"),
    )


def _kalshi_event_derived_link_condition() -> Any:
    return func.lower(SportsMarketLink.game_key).like("%kalshi-event-derived%")


def _upgraded_link_condition() -> Any:
    return or_(_verified_link_condition(), _kalshi_event_derived_link_condition())


def _count_sports_links(session: Session, *, ticker_prefix: str | None) -> int:
    statement = select(func.count()).select_from(SportsMarketLink).where(
        *_ticker_prefix_filters(SportsMarketLink.ticker, ticker_prefix)
    )
    return int(session.scalar(statement) or 0)


def _sports_link_provenance_counts(
    session: Session,
    *,
    ticker_prefix: str | None,
) -> Counter[str]:
    prefix_filters = _ticker_prefix_filters(SportsMarketLink.ticker, ticker_prefix)
    provenance = _sports_link_provenance_case().label("provenance")
    rows = session.execute(
        select(provenance, func.count(SportsMarketLink.id))
        .where(*prefix_filters)
        .group_by(provenance)
    )
    counts = Counter(
        {
            "verified_schedule": 0,
            "kalshi_event_derived": 0,
            "partial_market_derived": 0,
            "other": 0,
        }
    )
    for provenance_key, count in rows:
        counts[str(provenance_key)] = int(count or 0)
    return counts


def _sports_link_provenance_case() -> Any:
    reason = func.lower(SportsMarketLink.link_reason)
    game_key = func.lower(SportsMarketLink.game_key)
    return case(
        (reason.like("%verified schedule%"), "verified_schedule"),
        (game_key.like("%kalshi-event-derived%"), "kalshi_event_derived"),
        (
            game_key.like("%market-derived%") | reason.like("%market-derived%"),
            "partial_market_derived",
        ),
        else_="other",
    )


def _distinct_sports_link_tickers(
    session: Session,
    condition: Any,
    *,
    ticker_prefix: str | None,
) -> set[str]:
    statement = (
        select(SportsMarketLink.ticker)
        .where(condition, *_ticker_prefix_filters(SportsMarketLink.ticker, ticker_prefix))
        .distinct()
        .order_by(SportsMarketLink.ticker)
    )
    return set(session.scalars(statement))


def _raw_unresolved_partial_sports_tickers(
    session: Session,
    *,
    ticker_prefix: str | None,
) -> set[str]:
    upgraded_tickers = (
        select(SportsMarketLink.ticker)
        .where(
            _upgraded_link_condition(),
            *_ticker_prefix_filters(SportsMarketLink.ticker, ticker_prefix),
        )
        .distinct()
    )
    statement = (
        select(SportsMarketLink.ticker)
        .where(
            _partial_link_condition(),
            *_ticker_prefix_filters(SportsMarketLink.ticker, ticker_prefix),
            ~SportsMarketLink.ticker.in_(upgraded_tickers),
        )
        .distinct()
        .order_by(SportsMarketLink.ticker)
    )
    return set(session.scalars(statement))


def _sports_partial_tickers_excluded_from_repair(
    session: Session,
    tickers: set[str],
) -> set[str]:
    if not tickers:
        return set()
    sports_leg_tickers: set[str] = set()
    sorted_tickers = sorted(tickers)
    for index in range(0, len(sorted_tickers), 500):
        chunk = sorted_tickers[index : index + 500]
        sports_leg_tickers.update(
            session.scalars(
                select(MarketLeg.ticker)
                .where(
                    MarketLeg.category == CATEGORY_SPORTS,
                    MarketLeg.ticker.in_(chunk),
                )
                .distinct()
            )
        )
    return {
        ticker
        for ticker in tickers
        if _is_cross_category_ticker(ticker) or ticker not in sports_leg_tickers
    }


def _total_sports_parsed_markets(
    session: Session,
    *,
    coverage_sports: dict[str, Any],
    ticker_prefix: str | None,
) -> int:
    if ticker_prefix is None and coverage_sports.get("parsed_markets") is not None:
        return int(coverage_sports.get("parsed_markets") or 0)
    statement = (
        select(func.count(func.distinct(MarketLeg.ticker)))
        .where(
            MarketLeg.category == CATEGORY_SPORTS,
            *_ticker_prefix_filters(MarketLeg.ticker, ticker_prefix),
        )
    )
    return int(session.scalar(statement) or 0)


def _unlinked_sports_market_count(
    session: Session,
    *,
    coverage_sports: dict[str, Any],
    ticker_prefix: str | None,
) -> int:
    if ticker_prefix is None and coverage_sports:
        if coverage_sports.get("unlinked_markets") is not None:
            return int(coverage_sports.get("unlinked_markets") or 0)
        parsed_markets = coverage_sports.get("parsed_markets")
        linked_markets = (
            coverage_sports.get("external_linked_markets")
            or coverage_sports.get("linked_markets")
            or coverage_sports.get("usable_markets")
        )
        if parsed_markets is not None and linked_markets is not None:
            return max(int(parsed_markets or 0) - int(linked_markets or 0), 0)
    link_exists = (
        select(SportsMarketLink.id)
        .where(SportsMarketLink.ticker == MarketLeg.ticker)
        .limit(1)
        .exists()
    )
    statement = select(func.count(func.distinct(MarketLeg.ticker))).where(
        MarketLeg.category == CATEGORY_SPORTS,
        *_ticker_prefix_filters(MarketLeg.ticker, ticker_prefix),
        ~link_exists,
    )
    return int(session.scalar(statement) or 0)


def _unlinked_sports_tickers(
    session: Session,
    *,
    ticker_prefix: str | None,
    limit: int | None,
) -> set[str]:
    link_exists = (
        select(SportsMarketLink.id)
        .where(SportsMarketLink.ticker == MarketLeg.ticker)
        .limit(1)
        .exists()
    )
    statement = (
        select(MarketLeg.ticker)
        .where(
            MarketLeg.category == CATEGORY_SPORTS,
            *_ticker_prefix_filters(MarketLeg.ticker, ticker_prefix),
            ~link_exists,
        )
        .distinct()
        .order_by(MarketLeg.ticker)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return set(session.scalars(statement))


def _bounded_sorted_tickers(tickers: set[str], max_rows: int | None) -> set[str]:
    sorted_tickers = sorted(tickers)
    if max_rows is not None:
        sorted_tickers = sorted_tickers[:max_rows]
    return set(sorted_tickers)


def _row_scan(
    *,
    max_rows: int | None,
    ticker_prefix: str | None,
    unresolved_partial_tickers: set[str],
    unlinked_count: int,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_count = len(unresolved_partial_tickers) + unlinked_count
    complete = max_rows is None or len(rows) >= candidate_count
    return {
        "max_rows": max_rows,
        "ticker_prefix": ticker_prefix,
        "candidate_degraded_rows": candidate_count,
        "unresolved_partial_candidate_rows": len(unresolved_partial_tickers),
        "unlinked_candidate_rows": unlinked_count,
        "rows_materialized": len(rows),
        "complete": complete,
        "truncated": not complete,
    }


def _sports_links_for_tickers(
    session: Session,
    tickers: set[str],
) -> list[SportsMarketLink]:
    if not tickers:
        return []
    return list(
        session.scalars(
            select(SportsMarketLink)
            .where(SportsMarketLink.ticker.in_(sorted(tickers)))
            .order_by(SportsMarketLink.ticker, SportsMarketLink.id)
        )
    )


def _sports_legs_for_tickers(session: Session, tickers: set[str]) -> list[MarketLeg]:
    if not tickers:
        return []
    return list(
        session.scalars(
            select(MarketLeg)
            .where(
                MarketLeg.category == CATEGORY_SPORTS,
                MarketLeg.ticker.in_(sorted(tickers)),
            )
            .order_by(MarketLeg.ticker, MarketLeg.leg_index)
        )
    )


def _sports_games_for_links(
    session: Session,
    links: list[SportsMarketLink],
) -> dict[tuple[str, str], SportsGame]:
    game_keys = sorted({link.game_key for link in links if link.game_key})
    if not game_keys:
        return {}
    games = session.scalars(select(SportsGame).where(SportsGame.game_key.in_(game_keys)))
    return {(game.league, game.game_key): game for game in games}


def _links_by_ticker(links: list[SportsMarketLink]) -> dict[str, list[SportsMarketLink]]:
    grouped: dict[str, list[SportsMarketLink]] = defaultdict(list)
    for link in links:
        grouped[link.ticker].append(link)
    return dict(grouped)


def _legs_by_ticker(legs: list[MarketLeg]) -> dict[str, list[MarketLeg]]:
    grouped: dict[str, list[MarketLeg]] = defaultdict(list)
    for leg in legs:
        grouped[leg.ticker].append(leg)
    return dict(grouped)


def _markets_by_ticker(session: Session, tickers: set[str]) -> dict[str, Market]:
    if not tickers:
        return {}
    return {
        market.ticker: market
        for market in session.scalars(select(Market).where(Market.ticker.in_(sorted(tickers))))
    }


def _sports_games_by_key(session: Session) -> dict[tuple[str, str], SportsGame]:
    return {
        (game.league, game.game_key): game
        for game in session.scalars(
            select(SportsGame).order_by(SportsGame.league, SportsGame.game_key)
        )
    }


def _game_for_link(
    link: SportsMarketLink | None,
    games: dict[tuple[str, str], SportsGame],
) -> SportsGame | None:
    if link is None:
        return None
    return games.get((link.league, link.game_key))


def _link_provenance(link: SportsMarketLink) -> str:
    raw = decode_json(link.raw_json)
    source = str(raw.get("source") or "").lower()
    reason = str(link.link_reason or "").lower()
    game_key = str(link.game_key or "").lower()
    if source == "verified_schedule" or "verified schedule" in reason:
        return "verified_schedule"
    if "kalshi-event-derived" in game_key or source == "kalshi_event_derived":
        return "kalshi_event_derived"
    if (
        "market-derived" in game_key
        or source == "market-derived-fallback"
        or "market-derived" in reason
    ):
        return "partial_market_derived"
    return "other"


def _clean_market_type(link: SportsMarketLink | None, legs: list[MarketLeg]) -> str | None:
    if link and link.market_type:
        return link.market_type.upper()
    values = sorted({leg.market_type.upper() for leg in legs if leg.market_type})
    return values[0] if len(values) == 1 else "UNKNOWN"


def _infer_league(legs: list[MarketLeg]) -> str:
    text = " ".join([leg.raw_text for leg in legs]).lower()
    if any(term in text for term in ("dodgers", "red sox", "mlb", "runs scored")):
        return "MLB"
    if any(term in text for term in ("wnba", "caitlin", "aliyah")):
        return "WNBA"
    if any(term in text for term in ("goal", "vinicius", "brazil", "morocco")):
        return "SOCCER"
    return "UNKNOWN"


def _has_placeholder(
    link: SportsMarketLink | None,
    game: SportsGame | None,
    market: Market | None,
    legs: list[MarketLeg],
) -> bool:
    values: list[str] = []
    if link:
        values.extend([link.league, link.game_key, link.link_reason])
        values.extend(map(str, decode_json(link.matched_terms_json).values()))
    if game:
        values.extend([game.game_key, game.home_team_key, game.away_team_key, game.status])
        values.append(game.raw_json)
    if market:
        values.extend([market.ticker, market.title or "", market.subtitle or "", market.raw_json])
    values.extend([leg.entity_name or "" for leg in legs])
    values.extend([leg.raw_text for leg in legs])
    return any(PLACEHOLDER_RE.search(value or "") for value in values)


def _placeholder_example_tickers(payload: dict[str, Any] | list[Any] | None) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    rows = payload.get("placeholder_watch_rows") or payload.get("rows") or []
    tickers: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        tickers.update(str(item) for item in row.get("example_tickers") or [])
    return tickers


def _schedule_evidence(link: SportsMarketLink | None, game: SportsGame | None) -> str:
    if link and _link_provenance(link) == "verified_schedule":
        return "verified_schedule_link"
    if game:
        raw = decode_json(game.raw_json)
        source = str(raw.get("source") or raw.get("raw_json", {}).get("source") or "").lower()
        status = str(game.status or "").lower()
        if source == "verified_schedule" or status == "verified_schedule":
            return "verified_schedule_game"
        if source == "kalshi_event_derived" or status == "kalshi_event_derived":
            return "kalshi_event_derived"
    if link and _link_provenance(link) == "kalshi_event_derived":
        return "kalshi_event_derived"
    if link and _link_provenance(link) == "partial_market_derived":
        return "partial_market_derived"
    return "none"


def _clean_team_identity(
    game: SportsGame | None,
    schedule_evidence: str,
    placeholder_involved: bool,
) -> bool:
    if game is None or placeholder_involved:
        return False
    if not schedule_evidence.startswith("verified_schedule"):
        return False
    keys = (game.home_team_key or "", game.away_team_key or "")
    if not all(keys):
        return False
    return not any(SYNTHETIC_TEAM_RE.search(key) or PLACEHOLDER_RE.search(key) for key in keys)


def _is_cross_category_market(ticker: str, market: Market | None) -> bool:
    if _is_cross_category_ticker(ticker):
        return True
    if market is None:
        return False
    return "CROSSCATEGORY" in (market.event_ticker or "").upper()


def _is_cross_category_ticker(ticker: str) -> bool:
    return "CROSSCATEGORY" in ticker.upper()


def _ticker_family(ticker: str) -> str:
    if "-S" in ticker:
        return ticker.split("-S", 1)[0]
    if "-" in ticker:
        return ticker.split("-", 1)[0]
    return ticker


def _coverage_sports_row(payload: dict[str, Any] | list[Any] | None) -> dict[str, Any]:
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("coverage_rows") or payload.get("category_rows") or []
    else:
        rows = []
    for row in rows:
        if isinstance(row, dict) and (
            row.get("scope_key") == "sports" or row.get("category") == "sports"
        ):
            return row
    return {}


def _sports_coverage_row_from_inputs(
    report_inputs: dict[str, dict[str, Any] | list[Any] | None],
) -> dict[str, Any]:
    coverage_row = dict(_coverage_sports_row(report_inputs.get("coverage_rows")))
    link_row = _coverage_sports_row(report_inputs.get("link_coverage"))
    if not link_row:
        return coverage_row
    merged = dict(coverage_row)
    for key in (
        "parsed_legs",
        "parsed_markets",
        "partial_legs",
        "partial_link_rows",
        "partial_markets",
        "derived_usable_link_rows",
        "derived_usable_markets",
        "verified_schedule_link_rows",
        "verified_schedule_markets",
        "unlinked_markets",
    ):
        if link_row.get(key) is not None:
            merged[key] = link_row.get(key)
    if link_row.get("linked_markets") is not None:
        merged["external_linked_markets"] = link_row.get("linked_markets")
        merged["usable_markets"] = link_row.get("linked_markets")
    return merged


def _summary_payload(payload: dict[str, Any] | list[Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    summary = payload.get("summary")
    return summary if isinstance(summary, dict) else {}


def _phase3az_partial_count(payload: dict[str, Any] | list[Any] | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    for gap in payload.get("gaps") or []:
        if not isinstance(gap, dict) or gap.get("gap_id") != "sports_partial_provenance":
            continue
        match = re.search(r"(\d+)", str(gap.get("evidence") or ""))
        return int(match.group(1)) if match else None
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    gate = payload["phase3ae_gate"]
    lines = [
        "# Phase 3Z-R2 Sports Provenance Coverage Repair",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Auto-upgrades created: 0",
        f"- Phase 3AE gate: {gate['status']}",
        (
            f"- Row scan: reviewed {payload['row_scan']['rows_materialized']} of "
            f"{payload['row_scan']['candidate_degraded_rows']} candidate degraded row(s); "
            f"complete={payload['row_scan']['complete']}; "
            f"max_rows={payload['row_scan']['max_rows']}"
        ),
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Count Reconciliation",
            "",
        ]
    )
    for key, value in payload["count_reconciliation"]["values"].items():
        lines.append(f"- {key}: {value}")
    lines.append(f"- Explanation: {payload['count_reconciliation']['explanation']}")
    lines.extend(
        [
            "",
            "## Phase 3AE Gate",
            "",
            f"- Status: {gate['status']}",
            f"- Can run from this report: {gate['phase3ae_can_run_from_this_report']}",
            f"- Auto-upgrade allowed: {gate['auto_upgrade_allowed']}",
            f"- Next action: {gate['next_action']}",
            "",
            "## Grouped Degraded Rows",
            "",
            (
                "| Reason | Family | League | Type | Placeholder | Evidence | Safe | "
                "Count | Blockers | Examples |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for group in payload["grouped_degraded_links"][:80]:
        lines.append(
            f"| {_md(group['reason_code'])} | {_md(group['ticker_family'])} | "
            f"{_md(group['league'])} | {_md(group['market_type'])} | "
            f"{group['placeholder_involved']} | {_md(group['available_schedule_evidence'])} | "
            f"{group['safe_to_repair']} | {group['count']} | "
            f"{_md(', '.join(group['blocked_reasons']))} | "
            f"{_md(', '.join(group['example_tickers'][:5]))} |"
        )
    lines.extend(["", "## Next Commands", ""])
    for command in payload["next_commands"]:
        lines.append(f"- `{command}`")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)


def _md(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
