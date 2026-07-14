from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.orm import Session

import kalshi_predictor
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
    warn_if_sqlite_on_onedrive,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.maintenance import database_health, sqlite_backup
from kalshi_predictor.data.schema import (
    EconomicEvent,
    EconomicFeature,
    EconomicMarketLink,
    Market,
    MarketLeg,
    NewsFeature,
    NewsItem,
    NewsMarketLink,
    PaperFill,
    PaperOrder,
    PaperPnl,
    PaperPosition,
    Settlement,
)
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, ORDER_FILLED
from kalshi_predictor.paper.pnl import _settlement_result_for_pnl
from kalshi_predictor.paper.settlement_reconciliation import (
    PAPER_ONLY_SAFETY,
    build_paper_settlement_reconciliation,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE_3AM_VERSION = "phase3am_v1"
PHASE_3AM_BURNDOWN_VERSION = "phase3am_gap_burndown_v1"
SETTLED_PNL_NOTE = "settled market realized paper P&L"
PHASE3AY_READY_STATES = {"EXACT_SETTLEMENT_READY"}
PHASE3AM_BLOCKED_SETTLEMENT_STATES = {
    "AWAITING_EXACT_MARKET_SETTLEMENT",
    "MARKET_OUTCOME_MISSING",
    "COMPOSITE_LOCAL_REQUIRES_RESOLVER",
    "SIBLING_TICKER_REJECTED",
    "AMBIGUOUS_MATCH_REJECTED",
    "ALREADY_SETTLED",
    "DUPLICATE_OR_CONFLICTING",
    "REQUIRES_HUMAN_REVIEW",
}
OPEN_MARKET_STATUSES = {"active", "open", "initialized"}


@dataclass(frozen=True)
class Phase3AMArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class Phase3AMJsonArtifactSet:
    output_dir: Path
    json_path: Path


@dataclass(frozen=True)
class Phase3AMBurnDownArtifactSet:
    output_dir: Path
    summary_path: Path
    next_actions_path: Path
    burn_down_path: Path
    manifest_path: Path


def build_phase3am_runtime_identity(
    session: Session,
    *,
    settings: Settings | None = None,
    settlement_apply: bool = False,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    db_url = _session_db_url(session) or database_url_from_settings(resolved)
    redacted_url = redact_database_url(db_url)
    repo_root = _repo_root()
    writer = db_writer_monitor(settings=resolved, db_url=db_url)
    writer_safe = bool(writer.get("safe_to_start_write", True))
    sqlite_path = sqlite_path_from_url(db_url)
    file_backed_sqlite = sqlite_path is not None and str(sqlite_path) != ":memory:"
    allow_db_round_trips = writer_safe and (settlement_apply or not file_backed_sqlite)
    sqlite_identity = _sqlite_identity(
        session,
        db_url=db_url,
        settings=resolved,
        run_integrity=allow_db_round_trips,
        skip_reason=(
            "SKIPPED_ACTIVE_DB_WRITER" if not writer_safe else "SKIPPED_BOUNDED_PREFLIGHT"
        ),
    )
    health = (
        database_health(session, settings=resolved, db_url=db_url, include_integrity=False)
        if allow_db_round_trips
        else _database_health_skipped_for_bounded_preflight(resolved, db_url, writer=writer)
    )
    migration_revision = (
        _migration_revision(session)
        if allow_db_round_trips
        else ("SKIPPED_ACTIVE_DB_WRITER" if not writer_safe else "SKIPPED_BOUNDED_PREFLIGHT")
    )
    blockers = _preflight_blockers(
        health=health,
        sqlite_identity=sqlite_identity,
        writer=writer,
        settlement_apply=settlement_apply,
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AM",
        "phase_version": PHASE_3AM_BURNDOWN_VERSION,
        "mode": "PAPER_ONLY_PREFLIGHT",
        "safety_mode": "PAPER_READ_ONLY_UNLESS_EXACT_SETTLEMENT_APPLY",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "settlement_apply_requested": settlement_apply,
        "repository_root": str(repo_root),
        "git_branch": _git_value(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
        or "UNKNOWN_GIT_BRANCH",
        "git_commit": _git_value(repo_root, "rev-parse", "HEAD") or "UNKNOWN_GIT_COMMIT",
        "git_dirty": _git_dirty(repo_root),
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(kalshi_predictor.__file__).resolve()),
        "resolved_database_url": redacted_url,
        "database_location": describe_db_location(db_url),
        "database_fingerprint": _database_identity_fingerprint(redacted_url, db_url),
        "migration_revision": migration_revision,
        "ui_database_identity": _json_file_summary(
            Path("reports/phase3bb/phase3bb_workspace_guard.json")
        ),
        "cli_database_identity": {
            "database_url": redacted_url,
            "database_location": describe_db_location(db_url),
        },
        "worker_database_identity": _json_file_summary(
            Path("reports/phase3ay/phase3ay_status.json")
        ),
        "active_db_writer_status": writer,
        "database_health": health,
        "sqlite": sqlite_identity,
        "timezone": time.tzname[0] if time.tzname else "unknown",
        "safety_flags": {
            "live_trading_enabled": False,
            "demo_exchange_writes_enabled": False,
            "paper_default": True,
            "settlement_apply_requires_exact_only": True,
            "settlement_apply_requires_backup_first": True,
            "settlement_apply_requires_max_records": True,
        },
        "fail_closed": bool(blockers),
        "fail_closed_reasons": blockers,
    }


def write_phase3am_preflight_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3am"),
    settings: Settings | None = None,
    settlement_apply: bool = False,
) -> Phase3AMJsonArtifactSet:
    payload = build_phase3am_runtime_identity(
        session,
        settings=settings,
        settlement_apply=settlement_apply,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "runtime_identity.json"
    _write_json(json_path, payload)
    return Phase3AMJsonArtifactSet(output_dir, json_path)


def build_phase3ay_due_settlement_diagnostic(
    session: Session,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    reconciliation = build_paper_settlement_reconciliation(session, limit=limit)
    due_rows = [_due_trade_row(session, row) for row in _due_reconciliation_rows(reconciliation)]
    state_counts = _count_values(due_rows, "primary_state")
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AY",
        "phase_version": "phase3ay_due_settlement_diagnostic_v1",
        "mode": "READ_ONLY_EXACT_SETTLEMENT_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "exact_ticker_only": True,
        "sibling_tickers_allowed": False,
        "fuzzy_title_matching_allowed": False,
        "limit": limit,
        "summary": {
            "due_paper_trades": len(due_rows),
            "overdue_paper_trades": sum(
                1 for row in due_rows if row.get("close_time_bucket") == "overdue"
            ),
            "exact_market_matched_trades": sum(
                1 for row in due_rows if row.get("settlement_found")
            ),
            "exact_market_final_settled_trades": sum(
                1 for row in due_rows if row.get("primary_state") == "EXACT_SETTLEMENT_READY"
            ),
            "exact_market_not_yet_settled_trades": state_counts.get(
                "AWAITING_EXACT_MARKET_SETTLEMENT",
                0,
            ),
            "missing_market_outcome_trades": state_counts.get("MARKET_OUTCOME_MISSING", 0),
            "composite_local_trades": state_counts.get(
                "COMPOSITE_LOCAL_REQUIRES_RESOLVER",
                0,
            ),
            "sibling_ticker_candidates_rejected": sum(
                len(row.get("possible_settlement_matches") or []) for row in due_rows
            ),
            "ambiguous_candidates_rejected": state_counts.get("AMBIGUOUS_MATCH_REJECTED", 0),
            "already_settled_trades": state_counts.get("ALREADY_SETTLED", 0),
            "duplicate_conflicting_settlement_records": state_counts.get(
                "DUPLICATE_OR_CONFLICTING",
                0,
            ),
            "safe_to_apply_count": state_counts.get("EXACT_SETTLEMENT_READY", 0),
            "primary_state_counts": state_counts,
        },
        "rows": due_rows,
        "recommended_next_action": _due_diagnostic_next_action(state_counts),
    }


def write_phase3ay_due_settlement_diagnostic_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3am"),
    limit: int | None = None,
) -> Phase3AMJsonArtifactSet:
    payload = build_phase3ay_due_settlement_diagnostic(session, limit=limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "due_settlement_diagnostic.json"
    _write_json(json_path, payload)
    return Phase3AMJsonArtifactSet(output_dir, json_path)


def build_phase3ay_settle_due_paper(
    session: Session,
    *,
    settings: Settings | None = None,
    exact_only: bool = True,
    dry_run: bool = True,
    apply: bool = False,
    backup_first: bool = False,
    max_records: int = 5,
    output_dir: Path = Path("reports/phase3am"),
) -> dict[str, Any]:
    if not exact_only:
        raise ValueError("phase3ay-settle-due-paper requires --exact-only.")
    if max_records <= 0:
        raise ValueError("--max-records must be positive.")
    if apply and dry_run:
        raise ValueError("Use either --dry-run or --apply, not both.")
    if apply and not backup_first:
        raise ValueError("--apply requires --backup-first.")

    resolved = settings or get_settings()
    db_url = _session_db_url(session) or database_url_from_settings(resolved)
    writer = db_writer_monitor(settings=resolved, db_url=db_url)
    if apply and not bool(writer.get("safe_to_start_write", True)):
        raise RuntimeError("Settlement apply blocked: active database writer detected.")

    diagnostic = build_phase3ay_due_settlement_diagnostic(session, limit=max_records)
    ready_rows = [
        row for row in diagnostic["rows"] if row.get("primary_state") in PHASE3AY_READY_STATES
    ][:max_records]
    proposed_rows = [_settlement_proposal_row(session, row) for row in diagnostic["rows"]]
    proposed_ready = [row for row in proposed_rows if row.get("safe_to_apply")][:max_records]
    backup_path = None
    applied_rows: list[dict[str, Any]] = []

    if apply and proposed_ready:
        backup_path = sqlite_backup(
            output_path=output_dir
            / "backups"
            / f"phase3am_settlement_apply_{_timestamp_for_path()}.db",
            db_url=db_url,
        )
        now = utc_now()
        for row in proposed_ready:
            _apply_paper_pnl_row(session, row, calculated_at=now)
            applied = dict(row)
            applied["applied"] = True
            applied_rows.append(applied)
        session.flush()

    after_diagnostic = (
        build_phase3ay_due_settlement_diagnostic(session, limit=max_records) if apply else None
    )
    mutation = "INSERT paper_pnl row(s)" if apply and applied_rows else "NO_DATABASE_MUTATION"
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AY",
        "phase_version": "phase3ay_settle_due_paper_v1",
        "mode": "EXACT_SETTLEMENT_APPLY" if apply else "EXACT_SETTLEMENT_DRY_RUN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "exact_only": exact_only,
        "dry_run": dry_run,
        "apply": apply,
        "backup_first": backup_first,
        "backup_path": str(backup_path) if backup_path is not None else None,
        "max_records": max_records,
        "active_db_writer_status": writer,
        "summary": {
            "due_paper_trades": diagnostic["summary"]["due_paper_trades"],
            "exact_settlement_ready": len(ready_rows),
            "safe_to_apply_count": len(proposed_ready),
            "rows_applied": len(applied_rows),
            "database_mutation": mutation,
            "live_or_demo_execution": False,
            "sibling_tickers_used_for_settlement": 0,
            "fuzzy_title_matches_used": 0,
        },
        "before_diagnostic_summary": diagnostic["summary"],
        "after_diagnostic_summary": after_diagnostic["summary"] if after_diagnostic else None,
        "rows": proposed_rows,
        "applied_rows": applied_rows,
        "recommended_next_action": _settle_due_next_action(
            apply=apply,
            applied=len(applied_rows),
            safe=len(proposed_ready),
        ),
    }


def write_phase3ay_settle_due_paper_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3am"),
    settings: Settings | None = None,
    exact_only: bool = True,
    dry_run: bool = True,
    apply: bool = False,
    backup_first: bool = False,
    max_records: int = 5,
) -> Phase3AMJsonArtifactSet:
    payload = build_phase3ay_settle_due_paper(
        session,
        settings=settings,
        exact_only=exact_only,
        dry_run=dry_run,
        apply=apply,
        backup_first=backup_first,
        max_records=max_records,
        output_dir=output_dir,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / (
        "due_settlement_apply.json" if apply else "due_settlement_dry_run.json"
    )
    _write_json(json_path, payload)
    return Phase3AMJsonArtifactSet(output_dir, json_path)


def build_composite_settlement_classification(
    session: Session,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    diagnostic = build_phase3ay_due_settlement_diagnostic(session, limit=limit)
    rows = [
        _composite_classification_row(row)
        for row in diagnostic["rows"]
        if row.get("primary_state") == "COMPOSITE_LOCAL_REQUIRES_RESOLVER"
    ]
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AM",
        "phase_version": "phase3am_composite_classification_v1",
        "mode": "PAPER_ONLY_COMPOSITE_CLASSIFICATION",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "summary": {
            "composite_local_candidates": len(rows),
            "resolvable": sum(1 for row in rows if row["classification"] == "RESOLVABLE"),
            "requires_human_review": sum(
                1 for row in rows if row["classification"] == "REQUIRES_HUMAN_REVIEW"
            ),
            "settlements_applied": 0,
        },
        "rows": rows,
        "recommended_next_action": (
            "Run composite-settlement-resolve --paper-only --legacy-only --dry-run "
            "--max-records 5 --output-dir reports/phase3am if composite rows remain."
        ),
    }


def build_economic_news_market_watch(
    session: Session,
    *,
    handoff_limit: int = 25,
    readiness: dict[str, Any] | None = None,
    rebuild_readiness: bool = True,
) -> dict[str, Any]:
    now = utc_now()
    readiness_source = "provided"
    if readiness is None:
        if rebuild_readiness:
            from kalshi_predictor.phase3bb import build_phase3bb_domain_readiness

            readiness = build_phase3bb_domain_readiness(session)
            readiness_source = "live_rebuild"
        else:
            readiness = {"domain_rows": []}
            readiness_source = "not_rebuilt"
    domains = {
        row["domain"]: row
        for row in readiness.get("domain_rows", [])
        if isinstance(row, dict) and row.get("domain") in {"economic", "news"}
    }
    economic = domains.get("economic", {})
    news = domains.get("news", {})
    active_compatible = {
        "economic": _domain_count(economic, "active_parsed_markets"),
        "news": _domain_count(news, "active_parsed_markets"),
    }
    context_ready = {
        "economic": _context_ready_count(session, "economic"),
        "news": _context_ready_count(session, "news"),
    }
    current_handoff = _economic_news_current_market_handoff(
        session,
        now=now,
        sample_limit=handoff_limit,
    )
    economic_handoff = current_handoff["domains"]["economic"]
    news_handoff = current_handoff["domains"]["news"]
    return {
        "generated_at": now.isoformat(),
        "phase": "3AM",
        "phase_version": "phase3am_economic_news_watch_v1",
        "mode": "READ_ONLY_ECONOMIC_NEWS_COMPATIBLE_MARKET_WATCH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "summary": {
            "economic_compatible_active_markets": active_compatible["economic"],
            "news_compatible_active_markets": active_compatible["news"],
            "parsed_market_count": active_compatible["economic"] + active_compatible["news"],
            "context_ready_count": context_ready["economic"] + context_ready["news"],
            "economic_current_parsed_markets": economic_handoff["counts"][
                "current_parsed_markets"
            ],
            "news_current_parsed_markets": news_handoff["counts"]["current_parsed_markets"],
            "economic_exact_linked_current_markets": economic_handoff["counts"][
                "exact_linked_current_markets"
            ],
            "news_exact_linked_current_markets": news_handoff["counts"][
                "exact_linked_current_markets"
            ],
            "economic_exact_linked_current_without_parsed_leg": economic_handoff["counts"][
                "exact_linked_current_without_parsed_leg"
            ],
            "news_exact_linked_current_without_parsed_leg": news_handoff["counts"][
                "exact_linked_current_without_parsed_leg"
            ],
            "economic_current_handoff_blocker": economic_handoff["first_blocker"],
            "news_current_handoff_blocker": news_handoff["first_blocker"],
            "source_data_ready_waiting_for_compatible_markets": (
                context_ready["economic"] + context_ready["news"] > 0
                and (
                    economic_handoff["counts"]["exact_linked_current_markets"]
                    + news_handoff["counts"]["exact_linked_current_markets"]
                    == 0
                )
            ),
            "links_created": 0,
            "forecasts_created": 0,
            "live_or_demo_execution": False,
            "readiness_source": readiness_source,
        },
        "domains": domains,
        "readiness_source": readiness_source,
        "current_market_handoff": current_handoff,
        "blocked_reason": _economic_news_blocked_reason(economic, news),
        "next_refresh_time": None,
        "recommended_next_action": _economic_news_handoff_next_action(current_handoff),
    }


def write_economic_news_market_watch_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/economic_news_watch"),
) -> Phase3AMJsonArtifactSet:
    payload = build_economic_news_market_watch(session)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "economic_news_market_watch.json"
    _write_json(json_path, payload)
    return Phase3AMJsonArtifactSet(output_dir, json_path)


def build_phase3am_sports_gap_watch(*, reports_dir: Path = Path("reports")) -> dict[str, Any]:
    placeholder = _load_json(
        reports_dir / "phase3ah_sports" / "phase3ah_sports_placeholder_watch.json"
    )
    evidence = _load_json(
        reports_dir / "phase3ah_sports" / "phase3ah_sports_evidence_backfill.json"
    )
    phase3z = _load_json(
        reports_dir / "phase3z_r2" / "phase3z_r2_sports_provenance_repair.json"
    )
    placeholder_summary = _summary(placeholder)
    evidence_summary = _summary(evidence)
    z_summary = _summary(phase3z)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AM",
        "phase_version": "phase3am_sports_gap_watch_v1",
        "mode": "READ_ONLY_SPORTS_GAP_WATCH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "summary": {
            "placeholder_rows": int(placeholder_summary.get("placeholder_rows_reviewed") or 0),
            "unresolved_round_placeholders": int(
                placeholder_summary.get("still_placeholder_rows") or 0
            ),
            "partial_provenance_sports_markets": int(
                z_summary.get("partial_legacy_markets")
                or placeholder_summary.get("sports_partial_links_without_upgrade")
                or 0
            ),
            "schedule_evidence_available": bool(evidence_summary.get("schedule_windows")),
            "roster_team_evidence_available": bool(
                evidence_summary.get("roster_review_rows")
                or evidence_summary.get("team_alias_review_rows")
            ),
            "safe_repair_rows": int(z_summary.get("rows_safe_to_repair") or 0),
            "blocked_rows": int(z_summary.get("placeholder_blocked_rows") or 0),
            "auto_upgrades_created": 0,
            "feature_writes": False,
            "forecast_writes": False,
            "paper_trade_writes": False,
        },
        "reason_codes": _sports_reason_codes(placeholder_summary, evidence_summary, z_summary),
        "source_reports": {
            "placeholder_watch": str(
                reports_dir / "phase3ah_sports" / "phase3ah_sports_placeholder_watch.json"
            ),
            "sports_evidence": str(
                reports_dir / "phase3ah_sports" / "phase3ah_sports_evidence_backfill.json"
            ),
            "phase3z_r2": str(
                reports_dir / "phase3z_r2" / "phase3z_r2_sports_provenance_repair.json"
            ),
        },
        "recommended_next_action": (
            "Continue schedule/roster evidence gathering; do not treat placeholders as "
            "teams and do not upgrade partial links without provenance."
        ),
    }


def write_phase3am_gap_burndown_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3am"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    settlement_dry_run: bool = True,
    settlement_apply_exact_only: bool = False,
    backup_first: bool = False,
    max_settlements: int = 5,
) -> Phase3AMBurnDownArtifactSet:
    from kalshi_predictor.phase3ah_placeholder_watch import (
        write_phase3ah_sports_placeholder_watch_report,
    )
    from kalshi_predictor.phase3ah_sports import write_phase3ah_sports_evidence_report
    from kalshi_predictor.phase3az import write_phase3az_gap_analysis_report
    from kalshi_predictor.phase3bb import (
        write_phase3bb_general_source_availability_report,
        write_phase3bb_general_source_evidence_report,
        write_phase3bb_general_source_intake_report,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    phase3az_before = _load_json(reports_dir / "phase3az" / "phase3az_gap_analysis.json")
    preflight = build_phase3am_runtime_identity(
        session,
        settings=settings,
        settlement_apply=settlement_apply_exact_only,
    )
    _write_json(output_dir / "runtime_identity.json", preflight)

    diagnostic = build_phase3ay_due_settlement_diagnostic(session, limit=max_settlements)
    _write_json(output_dir / "due_settlement_diagnostic.json", diagnostic)
    dry_run = build_phase3ay_settle_due_paper(
        session,
        settings=settings,
        exact_only=True,
        dry_run=True,
        apply=False,
        backup_first=False,
        max_records=max_settlements,
        output_dir=output_dir,
    )
    _write_json(output_dir / "due_settlement_dry_run.json", dry_run)

    apply_payload = None
    if settlement_apply_exact_only:
        try:
            apply_payload = build_phase3ay_settle_due_paper(
                session,
                settings=settings,
                exact_only=True,
                dry_run=False,
                apply=True,
                backup_first=backup_first,
                max_records=max_settlements,
                output_dir=output_dir,
            )
        except Exception as exc:
            apply_payload = {
                "generated_at": utc_now().isoformat(),
                "phase": "3AY",
                "mode": "EXACT_SETTLEMENT_APPLY_BLOCKED",
                "apply": True,
                "backup_first": backup_first,
                "error": str(exc),
                "rows_applied": 0,
            }
        _write_json(output_dir / "due_settlement_apply.json", apply_payload)

    composite = build_composite_settlement_classification(session, limit=max_settlements)
    _write_json(output_dir / "composite_settlement_classification.json", composite)

    source_dir = reports_dir / "phase3bb_r2_sources"
    write_phase3bb_general_source_intake_report(session, output_dir=source_dir)
    write_phase3bb_general_source_evidence_report(session, output_dir=source_dir)
    write_phase3bb_general_source_availability_report(session, output_dir=source_dir)
    general_status = _general_source_evidence_status(reports_dir=reports_dir)
    _write_json(output_dir / "general_source_evidence_status.json", general_status)

    sports_dir = reports_dir / "phase3ah_sports"
    try:
        write_phase3ah_sports_evidence_report(session, output_dir=sports_dir)
    except FileNotFoundError as exc:
        _write_json(
            sports_dir / "phase3ah_sports_evidence_backfill.json",
            {
                "generated_at": utc_now().isoformat(),
                "phase": "3AH_SPORTS",
                "mode": "PAPER_ONLY_VERIFIED_SPORTS_EVIDENCE_BACKFILL_UNAVAILABLE",
                "paper_only_safety": PAPER_ONLY_SAFETY,
                "summary": {
                    "repair_rows_reviewed": 0,
                    "schedule_windows": 0,
                    "roster_review_rows": 0,
                    "team_alias_review_rows": 0,
                    "phase3ae_ready_rows": 0,
                    "auto_upgrades_created": 0,
                },
                "error": str(exc),
                "recommended_next_action": (
                    "Refresh Phase 3AG/3AH sports evidence inputs before attempting "
                    "sports upgrades."
                ),
            },
        )
    write_phase3ah_sports_placeholder_watch_report(output_dir=sports_dir)
    sports = build_phase3am_sports_gap_watch(reports_dir=reports_dir)
    _write_json(output_dir / "sports_gap_watch.json", sports)

    economic = build_economic_news_market_watch(session)
    _write_json(output_dir / "economic_news_watch.json", economic)
    econ_dir = reports_dir / "economic_news_watch"
    econ_dir.mkdir(parents=True, exist_ok=True)
    _write_json(econ_dir / "economic_news_market_watch.json", economic)

    after_artifacts = write_phase3az_gap_analysis_report(
        output_dir=reports_dir / "phase3az",
        reports_dir=reports_dir,
    )
    phase3az_after = _load_json(after_artifacts.json_path)
    burn = _phase3az_burndown_payload(
        before=phase3az_before,
        after=phase3az_after,
        diagnostic=diagnostic,
        dry_run=dry_run,
        apply_payload=apply_payload,
        general_status=general_status,
        sports=sports,
        economic=economic,
    )
    burn_path = output_dir / "phase3az_gap_burndown.json"
    _write_json(burn_path, burn)
    summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    summary_path.write_text(_render_burndown_summary(burn), encoding="utf-8")
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    next_actions_path.write_text(_render_phase3am_next_actions(burn), encoding="utf-8")
    manifest_path = output_dir / "MANIFEST.sha256"
    _write_manifest(
        manifest_path,
        [
            output_dir / "runtime_identity.json",
            output_dir / "due_settlement_diagnostic.json",
            output_dir / "due_settlement_dry_run.json",
            output_dir / "composite_settlement_classification.json",
            output_dir / "general_source_evidence_status.json",
            output_dir / "sports_gap_watch.json",
            output_dir / "economic_news_watch.json",
            burn_path,
            summary_path,
            next_actions_path,
            *( [output_dir / "due_settlement_apply.json"] if settlement_apply_exact_only else [] ),
        ],
    )
    return Phase3AMBurnDownArtifactSet(
        output_dir,
        summary_path,
        next_actions_path,
        burn_path,
        manifest_path,
    )


def build_phase3am_sports_verified_upgrade(
    session: Session,
    *,
    settings: Settings | None = None,
    upgrade_verified: bool = False,
    apply_aliases: bool = False,
    limit: int | None = None,
    min_confidence: Decimal | None = None,
) -> dict[str, Any]:
    """Separate sports link provenance and optionally run conservative upgrades."""
    from kalshi_predictor.market_legs import link_coverage_dashboard
    from kalshi_predictor.phase3ae import run_verified_sports_schedule_connector
    from kalshi_predictor.phase3aj import build_sports_alias_provenance_repair

    resolved = settings or get_settings()
    session.flush()
    before = link_coverage_dashboard(session)
    alias_repair = build_sports_alias_provenance_repair(
        session,
        limit=limit,
        apply_aliases=apply_aliases,
    )
    verified_upgrade: dict[str, Any] = {"status": "SKIPPED", "reason": "upgrade_verified=false"}
    if upgrade_verified:
        verified_upgrade = run_verified_sports_schedule_connector(
            session,
            settings=resolved,
            limit=limit,
            min_confidence=min_confidence,
            build_features=True,
            refresh_features=False,
        )
        session.flush()
    after = link_coverage_dashboard(session)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AM",
        "phase_version": PHASE_3AM_VERSION,
        "mode": "PAPER_ONLY_SPORTS_VERIFIED_UPGRADE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "upgrade_verified": upgrade_verified,
        "apply_aliases": apply_aliases,
        "before_sports": _sports_reconciliation(before),
        "after_sports": _sports_reconciliation(after),
        "provenance_rows": _provenance_rows(_sports_reconciliation(after)),
        "alias_repair_summary": alias_repair["summary"],
        "alias_suggestions": alias_repair.get("alias_suggestions", [])[:50],
        "competition_suggestions": alias_repair.get("competition_suggestions", [])[:50],
        "multi_leg_examples": [
            row
            for row in alias_repair.get("rows", [])
            if row.get("multi_leg")
            or row.get("reason") == "MULTI_LEG_MARKET_REQUIRES_COMPETITION_PROVENANCE"
        ][:50],
        "verified_upgrade_summary": _verified_summary(verified_upgrade),
        "recommended_next_action": _next_action(_sports_reconciliation(after), alias_repair),
    }


def write_phase3am_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3am"),
    settings: Settings | None = None,
    upgrade_verified: bool = False,
    apply_aliases: bool = False,
    limit: int | None = None,
    min_confidence: Decimal | None = None,
) -> Phase3AMArtifactSet:
    payload = build_phase3am_sports_verified_upgrade(
        session,
        settings=settings,
        upgrade_verified=upgrade_verified,
        apply_aliases=apply_aliases,
        limit=limit,
        min_confidence=min_confidence,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3am_sports_verified_upgrade.json"
    markdown_path = output_dir / "phase3am_sports_verified_upgrade.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AMArtifactSet(output_dir, json_path, markdown_path)


def _sports_reconciliation(dashboard: dict[str, Any]) -> dict[str, Any]:
    return dashboard.get("reconciliation", {}).get("sports", {})


def _provenance_rows(sports: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": "verified_schedule_links",
            "value": sports.get("verified_schedule_link_rows", 0),
            "trust": "verified",
            "learning_policy": "eligible when all component legs and snapshots are usable",
        },
        {
            "name": "derived_but_usable_links",
            "value": sports.get("derived_usable_link_rows", 0),
            "trust": "derived",
            "learning_policy": "allowed only through Phase 3AK component gate",
        },
        {
            "name": "partial_link_rows",
            "value": sports.get("partial_link_rows", 0),
            "trust": "partial",
            "learning_policy": "diagnostics only; excluded from learning",
        },
    ]


def _verified_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("status") == "SKIPPED":
        return payload
    summary = payload.get("summary", {})
    return {
        "status": "RAN",
        "verified_links_created": summary.get("verified_links_created", 0),
        "verified_links_existing": summary.get("verified_links_existing", 0),
        "features_created": summary.get("features_created", 0),
        "remaining_partial_without_upgrade": summary.get("remaining_partial_without_upgrade", 0),
    }


def _next_action(sports: dict[str, Any], alias_repair: dict[str, Any]) -> str:
    unresolved = int(sports.get("unresolved_partial_markets") or 0)
    soccer = int(alias_repair.get("summary", {}).get("soccer_markets", 0) or 0)
    if unresolved and soccer:
        return "Ingest or enter verified soccer competition schedules, then rerun Phase 3AM."
    if unresolved:
        return "Run Phase 3AM with --upgrade-verified after schedule/team data is fresh."
    return "Sports partial links are separated from verified and derived provenance."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AM Sports Verified Upgrade",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Upgrade verified: {payload['upgrade_verified']}",
        f"- Apply aliases: {payload['apply_aliases']}",
        "",
        "## Provenance Separation",
        "",
        "| Group | Value | Trust | Learning policy |",
        "| --- | ---: | --- | --- |",
    ]
    for row in payload["provenance_rows"]:
        lines.append(
            f"| {row['name']} | {row['value']} | {row['trust']} | "
            f"{row['learning_policy']} |"
        )
    lines.extend(
        [
            "",
            "## Alias / Competition Repair Summary",
            "",
        ]
    )
    for key, value in payload["alias_repair_summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Multi-Leg Examples",
            "",
            "| Ticker | Reason | Next action |",
            "| --- | --- | --- |",
        ]
    )
    for row in payload["multi_leg_examples"][:20]:
        lines.append(
            f"| `{row['ticker']}` | {row.get('reason')} | "
            f"{_md(row.get('next_action'))} |"
        )
    if not payload["multi_leg_examples"]:
        lines.append("| none |  |  |")
    lines.extend(["", "## Verified Upgrade Summary", ""])
    for key, value in payload["verified_upgrade_summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)


def _md(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _session_db_url(session: Session) -> str | None:
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    return str(url) if url is not None else None


def _repo_root() -> Path:
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / ".git").exists():
            return parent
    return path.parents[2]


def _git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _git_dirty(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    if result.returncode != 0:
        return "UNKNOWN"
    return "DIRTY" if result.stdout.strip() else "CLEAN"


def _database_identity_fingerprint(redacted_url: str, db_url: str) -> str:
    payload = json.dumps(
        {"database_url": redacted_url, "location": describe_db_location(db_url)},
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _database_health_skipped_for_bounded_preflight(
    settings: Settings,
    db_url: str,
    *,
    writer: dict[str, Any],
) -> dict[str, Any]:
    warning = warn_if_sqlite_on_onedrive(settings, db_url=db_url)
    writer_active = not bool(writer.get("safe_to_start_write", True))
    message = (
        "Database round-trip checks skipped because an active writer is holding "
        "the SQLite database; rerun preflight after the writer finishes for the "
        "full migration and quick-status check."
        if writer_active
        else (
            "Database round-trip checks skipped for bounded read-only preflight on "
            "file-backed SQLite; run db-health when no writer-sensitive job is active "
            "for the full migration and quick-status check."
        )
    )
    migration_message = (
        "Migration query skipped while active writer is present."
        if writer_active
        else "Migration query skipped by bounded read-only preflight."
    )
    return {
        "status": "WARNING",
        "items": [
            {
                "name": "DB health",
                "status": "WARNING",
                "message": message,
            },
            {
                "name": "SQLite OneDrive safety",
                "status": "WARNING" if warning else "READY",
                "message": warning or "No SQLite OneDrive warning.",
            },
            {
                "name": "Alembic migrations",
                "status": "WARNING",
                "message": migration_message,
            },
        ],
        "summary": {
            "backend": "sqlite",
            "backend_label": "sqlite",
            "database_url": redact_database_url(db_url),
            "location": describe_db_location(db_url),
            "sqlite_on_onedrive_warning": warning,
            "migration": {
                "status": "WARNING",
                "message": migration_message,
            },
        },
        "recovery": "",
    }


def _migration_revision(session: Session) -> str | None:
    try:
        exists = session.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='alembic_version'"
            )
        ).first()
        if exists is None:
            return None
        revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        return None
    return str(revision) if revision is not None else None


def _sqlite_identity(
    session: Session,
    *,
    db_url: str,
    settings: Settings,
    run_integrity: bool = True,
    skip_reason: str = "SKIPPED_ACTIVE_DB_WRITER",
) -> dict[str, Any]:
    path = sqlite_path_from_url(db_url)
    if path is None:
        return {"backend": "not_sqlite", "integrity_check": None}
    if str(path) == ":memory:":
        return {"backend": "sqlite", "path": ":memory:", "integrity_check": "ok"}
    exists = path.exists()
    identity: dict[str, Any] = {
        "backend": "sqlite",
        "path": str(path),
        "exists": exists,
        "onedrive_warning": warn_if_sqlite_on_onedrive(settings, db_url=db_url),
    }
    if not exists:
        identity.update({"integrity_check": "missing", "fingerprint": None})
        return identity
    stat = path.stat()
    identity.update(
        {
            "file_size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "fingerprint": _bounded_file_fingerprint(path),
            "fingerprint_mode": "sha256:size_mtime_first_last_1mb",
        }
    )
    if not run_integrity:
        identity["integrity_check"] = skip_reason
        return identity
    try:
        result = session.execute(text("PRAGMA integrity_check(1)")).scalar()
    except Exception:
        try:
            with sqlite3.connect(path) as connection:
                result = connection.execute("PRAGMA integrity_check(1)").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            result = str(exc)
    identity["integrity_check"] = str(result)
    return identity


def _bounded_file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    stat = path.stat()
    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            handle.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return "sha256:" + digest.hexdigest()


def _json_file_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "NOT_AVAILABLE", "path": str(path)}
    payload = _load_json(path)
    return {
        "status": "AVAILABLE" if payload else "UNREADABLE",
        "path": str(path),
        "generated_at": payload.get("generated_at") if isinstance(payload, dict) else None,
        "summary": payload.get("summary") if isinstance(payload, dict) else None,
    }


def _preflight_blockers(
    *,
    health: dict[str, Any],
    sqlite_identity: dict[str, Any],
    writer: dict[str, Any],
    settlement_apply: bool,
) -> list[str]:
    blockers: list[str] = []
    if health.get("status") == "BLOCKED":
        blockers.append("DATABASE_HEALTH_BLOCKED")
    integrity = str(sqlite_identity.get("integrity_check") or "").lower()
    allowed_integrity_states = {
        "ok",
        "skipped_active_db_writer",
        "skipped_bounded_preflight",
    }
    if integrity and integrity not in allowed_integrity_states:
        blockers.append("SQLITE_INTEGRITY_CHECK_NOT_OK")
    if settlement_apply and not bool(writer.get("safe_to_start_write", True)):
        blockers.append("ACTIVE_DB_WRITER_BLOCKS_SETTLEMENT_APPLY")
    return blockers


def _due_reconciliation_rows(reconciliation: dict[str, Any]) -> list[dict[str, Any]]:
    rows = reconciliation.get("rows") if isinstance(reconciliation, dict) else []
    due_rows: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if row.get("status") != ORDER_FILLED:
            continue
        if row.get("close_time_bucket") in {"overdue", "0-6h"} or row.get(
            "eligible_to_settle_now"
        ):
            due_rows.append(row)
    return due_rows


def _due_trade_row(session: Session, row: dict[str, Any]) -> dict[str, Any]:
    state = _primary_settlement_state(row)
    financials = _trade_financials(session, row)
    return {
        **row,
        "primary_state": state,
        "safe_to_apply": state == "EXACT_SETTLEMENT_READY"
        and financials.get("payout") is not None,
        "blocker": None if state == "EXACT_SETTLEMENT_READY" else _state_blocker(state),
        **financials,
    }


def _primary_settlement_state(row: dict[str, Any]) -> str:
    reason = str(row.get("reason") or "")
    if reason == "ELIGIBLE_TO_SETTLE_NOW":
        return "EXACT_SETTLEMENT_READY"
    if reason == "ALREADY_REALIZED":
        return "ALREADY_SETTLED"
    if reason == "LOCAL_DERIVED_COMPOSITE_NO_EXACT_SETTLEMENT":
        return "COMPOSITE_LOCAL_REQUIRES_RESOLVER"
    if reason == "SIBLING_DIFFERENT_CONTRACT_LEG":
        return "SIBLING_TICKER_REJECTED"
    if reason in {"VALIDATED_SIBLING_REQUIRES_MANUAL_REVIEW", "POSSIBLE_TICKER_MISMATCH"}:
        return "AMBIGUOUS_MATCH_REJECTED"
    if reason in {"NO_SETTLEMENT_YET", "MARKET_STILL_OPEN"}:
        return "AWAITING_EXACT_MARKET_SETTLEMENT"
    if reason in {"SETTLEMENT_RESULT_UNUSABLE", "MISSING_ENTRY_PRICE"}:
        return "MARKET_OUTCOME_MISSING"
    if reason in {"SIDE_MAPPING_UNSUPPORTED", "MALFORMED_TICKER", "ORDER_NOT_FILLED"}:
        return "REQUIRES_HUMAN_REVIEW"
    return "REQUIRES_HUMAN_REVIEW"


def _state_blocker(state: str) -> str:
    blockers = {
        "AWAITING_EXACT_MARKET_SETTLEMENT": "No final exact-ticker settlement row is available.",
        "MARKET_OUTCOME_MISSING": "Exact settlement exists but lacks a usable final outcome.",
        "COMPOSITE_LOCAL_REQUIRES_RESOLVER": (
            "Composite/local ticker requires the guarded composite resolver."
        ),
        "SIBLING_TICKER_REJECTED": "Only sibling/different-leg settlement candidates exist.",
        "AMBIGUOUS_MATCH_REJECTED": "Nearby or same-leg sibling candidates require review.",
        "ALREADY_SETTLED": "Latest paper P&L already reflects this exact settlement.",
        "DUPLICATE_OR_CONFLICTING": "Duplicate or conflicting settlement records were detected.",
        "REQUIRES_HUMAN_REVIEW": "The row is outside the deterministic exact-settlement mapper.",
    }
    return blockers.get(state, "Settlement is blocked by the exact-ticker safety policy.")


def _trade_financials(session: Session, row: dict[str, Any]) -> dict[str, Any]:
    order_id = row.get("paper_order_id")
    order = session.get(PaperOrder, order_id) if order_id is not None else None
    if order is None:
        return {"payout": None, "realized_pnl": None, "roi": None}
    entry_price = _entry_price(session, order)
    fees = _fees_for_order(session, int(order.id))
    outcome = to_decimal(row.get("settlement_outcome"))
    if outcome is None:
        outcome = _settlement_outcome(session.get(Settlement, order.ticker))
    cost = entry_price * order.quantity + fees if entry_price is not None else None
    payout = None
    realized = None
    roi = None
    if outcome is not None and entry_price is not None:
        payout = (
            outcome * order.quantity
            if order.side.upper() == BUY_YES
            else (Decimal("1") - outcome) * order.quantity
        )
        realized = payout - cost
        roi = None if cost == 0 else realized / cost
    return {
        "entry_price": decimal_to_str(entry_price),
        "fees": decimal_to_str(fees) or "0",
        "final_outcome": decimal_to_str(outcome),
        "payout": decimal_to_str(payout),
        "realized_pnl": decimal_to_str(realized),
        "roi": decimal_to_str(roi),
        "idempotency_key": _idempotency_key(order, row, outcome),
        "proposed_ledger_mutation": (
            "INSERT paper_pnl settled-market row"
            if payout is not None
            else "NO_MUTATION_BLOCKED"
        ),
    }


def _entry_price(session: Session, order: PaperOrder) -> Decimal | None:
    fills = list(
        session.scalars(
            select(PaperFill)
            .where(PaperFill.paper_order_id == order.id)
            .order_by(PaperFill.filled_at, PaperFill.id)
        )
    )
    if fills:
        total_quantity = sum(fill.quantity for fill in fills)
        if total_quantity > 0:
            total_cost = sum(
                (to_decimal(fill.price) or Decimal("0")) * fill.quantity for fill in fills
            )
            return total_cost / total_quantity
    return to_decimal(order.market_price) or to_decimal(order.limit_price)


def _fees_for_order(session: Session, order_id: int) -> Decimal:
    fees = session.scalars(select(PaperFill.fee).where(PaperFill.paper_order_id == order_id))
    return sum((to_decimal(fee) or Decimal("0") for fee in fees), Decimal("0"))


def _settlement_outcome(settlement: Settlement | None) -> Decimal | None:
    if settlement is None:
        return None
    value = to_decimal(settlement.yes_settlement_value)
    if value is not None and Decimal("0") <= value <= Decimal("1"):
        return value
    result = (settlement.result or "").strip().lower()
    if result in {"yes", "y", "1", "true"}:
        return Decimal("1")
    if result in {"no", "n", "0", "false"}:
        return Decimal("0")
    return None


def _idempotency_key(
    order: PaperOrder,
    row: dict[str, Any],
    outcome: Decimal | None,
) -> str:
    payload = {
        "phase": "phase3am",
        "paper_order_id": order.id,
        "ticker": order.ticker,
        "side": order.side,
        "quantity": order.quantity,
        "settled_at": row.get("settled_at"),
        "outcome": decimal_to_str(outcome),
    }
    return "phase3am:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _settlement_proposal_row(session: Session, row: dict[str, Any]) -> dict[str, Any]:
    proposal = dict(row)
    order = session.get(PaperOrder, row.get("paper_order_id"))
    settlement = session.get(Settlement, row.get("ticker"))
    proposal.update(
        {
            "paper_trade_id": row.get("paper_order_id"),
            "exact_market_ticker": row.get("ticker"),
            "settlement_result": settlement.result if settlement is not None else None,
            "yes_settlement_value": settlement.yes_settlement_value
            if settlement is not None
            else None,
            "safe_to_apply": bool(row.get("safe_to_apply"))
            and order is not None
            and settlement is not None,
            "blocker": row.get("blocker"),
        }
    )
    return proposal


def _apply_paper_pnl_row(
    session: Session,
    row: dict[str, Any],
    *,
    calculated_at: datetime,
) -> None:
    order = session.get(PaperOrder, row["paper_trade_id"])
    settlement = session.get(Settlement, row["exact_market_ticker"])
    if order is None or settlement is None:
        raise RuntimeError("Cannot apply settlement without exact order and settlement rows.")
    entry = to_decimal(row.get("entry_price")) or Decimal("0")
    realized = to_decimal(row.get("realized_pnl")) or Decimal("0")
    if order.side.upper() == BUY_YES:
        yes_contracts = order.quantity
        no_contracts = 0
        avg_yes = entry
        avg_no = None
    elif order.side.upper() == BUY_NO:
        yes_contracts = 0
        no_contracts = order.quantity
        avg_yes = None
        avg_no = entry
    else:
        raise RuntimeError(f"Unsupported side for exact settlement apply: {order.side}")
    session.add(
        PaperPnl(
            ticker=order.ticker,
            calculated_at=calculated_at,
            yes_contracts=yes_contracts,
            no_contracts=no_contracts,
            avg_yes_price=decimal_to_str(avg_yes),
            avg_no_price=decimal_to_str(avg_no),
            settlement_result=_settlement_result_for_pnl(settlement),
            realized_pnl=decimal_to_str(realized) or "0",
            unrealized_pnl="0",
            total_pnl=decimal_to_str(realized) or "0",
            notes=SETTLED_PNL_NOTE,
        )
    )
    position = session.get(PaperPosition, order.ticker)
    if position is not None:
        position.realized_pnl = decimal_to_str(realized) or "0"
        position.updated_at = calculated_at
        session.add(position)


def _due_diagnostic_next_action(state_counts: dict[str, int]) -> str:
    if state_counts.get("EXACT_SETTLEMENT_READY"):
        return (
            "Dry-run exact paper settlement, then apply with --backup-first only after "
            "reviewing the proposed ledger rows."
        )
    if state_counts.get("COMPOSITE_LOCAL_REQUIRES_RESOLVER"):
        return "Run the guarded composite resolver dry-run; do not force ordinary settlement."
    return "Keep Phase 3AY exact-ticker settlement watch running."


def _settle_due_next_action(*, apply: bool, applied: int, safe: int) -> str:
    if apply and applied:
        return "Rerun phase3az-gap-analysis and review the remaining due-paper gap."
    if apply:
        return "No exact settlement rows were applied; review blockers in this report."
    if safe:
        return (
            "Dry-run found exact rows. Rerun with --apply --backup-first --exact-only "
            "after review."
        )
    return "No due paper trade is exact-settlement-ready; keep the watch running."


def _composite_classification_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper_trade_id": row.get("paper_order_id"),
        "ticker": row.get("ticker"),
        "classification": "LOCAL_ONLY_NO_CANONICAL_RULE",
        "primary_state": row.get("primary_state"),
        "reason": row.get("reason"),
        "settlement_applied": False,
        "next_action": (
            "Use composite-settlement-resolve in dry-run mode and require explicit "
            "deterministic component rules before any apply."
        ),
    }


def _domain_count(domain: dict[str, Any], key: str) -> int:
    counts = domain.get("counts") if isinstance(domain, dict) else {}
    if not isinstance(counts, dict):
        return 0
    return int(counts.get(key) or counts.get("parsed_markets") or 0)


def _context_ready_count(session: Session, domain: str) -> int:
    if domain == "economic":
        return int(session.scalar(select(func.count(EconomicEvent.event_key))) or 0) + int(
            session.scalar(select(func.count(EconomicFeature.id))) or 0
        )
    return int(session.scalar(select(func.count(NewsItem.id))) or 0) + int(
        session.scalar(select(func.count(NewsFeature.id))) or 0
    )


def _economic_news_current_market_handoff(
    session: Session,
    *,
    now: datetime,
    sample_limit: int = 25,
) -> dict[str, Any]:
    return {
        "generated_at": now.isoformat(),
        "scope": {
            "requires_exact_ticker": True,
            "requires_parsed_market_leg": True,
            "requires_open_status": sorted(OPEN_MARKET_STATUSES),
            "requires_close_time_after_generated_at": True,
            "requires_expected_expiration_not_passed": True,
            "sample_limit_per_domain": sample_limit,
            "paper_only": True,
            "fuzzy_or_sibling_matching": False,
        },
        "domains": {
            "economic": _economic_news_handoff_domain(
                session, "economic", now=now, limit=sample_limit
            ),
            "news": _economic_news_handoff_domain(session, "news", now=now, limit=sample_limit),
        },
    }


def _economic_news_handoff_domain(
    session: Session,
    domain: str,
    *,
    now: datetime,
    limit: int,
) -> dict[str, Any]:
    current_parsed = _current_parsed_market_count(session, domain, now=now)
    current_linked = _exact_linked_current_market_count(session, domain, now=now)
    current_linked_any = _exact_linked_current_market_count(
        session,
        domain,
        now=now,
        require_parsed_leg=False,
    )
    parsed_total = _parsed_market_count(session, domain)
    linked_total = _linked_market_count_for_domain(session, domain)
    rows = _economic_news_handoff_rows(session, domain, now=now, limit=limit)
    link_only_rows = _economic_news_link_only_current_rows(
        session,
        domain,
        now=now,
        limit=limit,
    )
    non_current = max(0, parsed_total - current_parsed)
    missing_exact_link = max(0, current_parsed - current_linked)
    linked_not_current = max(0, linked_total - current_linked_any)
    linked_current_without_parse = max(0, current_linked_any - current_linked)
    first_blocker = _economic_news_handoff_blocker(
        current_parsed=current_parsed,
        current_linked=current_linked,
        linked_current_without_parse=linked_current_without_parse,
        non_current=non_current,
        missing_exact_link=missing_exact_link,
    )
    return {
        "domain": domain,
        "first_blocker": first_blocker,
        "counts": {
            "parsed_markets_total": parsed_total,
            "current_parsed_markets": current_parsed,
            "non_current_parsed_markets": non_current,
            "exact_linked_markets_total": linked_total,
            "exact_linked_current_markets": current_linked,
            "exact_linked_current_markets_any_parse_state": current_linked_any,
            "exact_linked_current_without_parsed_leg": linked_current_without_parse,
            "exact_linked_not_current_markets": linked_not_current,
            "current_parsed_missing_exact_link": missing_exact_link,
            "sample_rows": len(rows),
            "link_only_sample_rows": len(link_only_rows),
        },
        "rows": rows,
        "link_only_rows": link_only_rows,
    }


def _market_current_conditions(now: datetime) -> tuple[Any, ...]:
    return (
        func.lower(func.coalesce(Market.status, "")).in_(OPEN_MARKET_STATUSES),
        Market.close_time.is_not(None),
        Market.close_time > now,
        or_(Market.expected_expiration_time.is_(None), Market.expected_expiration_time > now),
        Market.settlement_ts.is_(None),
        or_(Market.result.is_(None), Market.result == ""),
    )


def _parsed_market_count(session: Session, domain: str) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(MarketLeg.ticker))).where(
                MarketLeg.category == domain
            )
        )
        or 0
    )


def _current_parsed_market_count(session: Session, domain: str, *, now: datetime) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(MarketLeg.ticker)))
            .join(Market, Market.ticker == MarketLeg.ticker)
            .where(MarketLeg.category == domain, *_market_current_conditions(now))
        )
        or 0
    )


def _linked_market_count_for_domain(session: Session, domain: str) -> int:
    link_model = EconomicMarketLink if domain == "economic" else NewsMarketLink
    return int(session.scalar(select(func.count(func.distinct(link_model.ticker)))) or 0)


def _exact_linked_current_market_count(
    session: Session,
    domain: str,
    *,
    now: datetime,
    require_parsed_leg: bool = True,
) -> int:
    link_model = EconomicMarketLink if domain == "economic" else NewsMarketLink
    statement = (
        select(func.count(func.distinct(Market.ticker)))
        .join(link_model, link_model.ticker == Market.ticker)
        .where(*_market_current_conditions(now))
    )
    if require_parsed_leg:
        statement = statement.join(MarketLeg, MarketLeg.ticker == Market.ticker).where(
            MarketLeg.category == domain
        )
    return int(
        session.scalar(statement)
        or 0
    )


def _economic_news_handoff_rows(
    session: Session,
    domain: str,
    *,
    now: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Market, MarketLeg)
        .join(MarketLeg, MarketLeg.ticker == Market.ticker)
        .where(MarketLeg.category == domain)
        .order_by(desc(Market.last_seen_at), desc(MarketLeg.parsed_at), Market.ticker)
        .limit(limit * 4)
    ).all()
    tickers = [str(row.Market.ticker) for row in rows]
    links = _latest_link_rows(session, domain, tickers)
    examples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        market = row.Market
        leg = row.MarketLeg
        ticker = str(market.ticker)
        if ticker in seen:
            continue
        seen.add(ticker)
        link = links.get(ticker)
        is_current, current_reason = _is_current_market(market, now=now)
        reason_codes: list[str] = []
        if not is_current:
            reason_codes.append(current_reason)
        if link is None:
            reason_codes.append("MISSING_EXACT_LINK")
        if is_current and link is not None:
            reason_codes.append("EXACT_CURRENT_COMPATIBLE")
        examples.append(
            {
                "ticker": ticker,
                "title": market.title,
                "status": market.status,
                "event_ticker": market.event_ticker,
                "series_ticker": market.series_ticker,
                "close_time": _iso_or_none(market.close_time),
                "expected_expiration_time": _iso_or_none(market.expected_expiration_time),
                "last_seen_at": _iso_or_none(market.last_seen_at),
                "leg_parsed_at": _iso_or_none(leg.parsed_at),
                "leg_text": leg.raw_text,
                "market_type": leg.market_type,
                "exact_link_present": link is not None,
                "link_reference": _link_reference(domain, link),
                "is_current_market": is_current,
                "reason_codes": reason_codes,
            }
        )
        if len(examples) >= limit:
            break
    return examples


def _economic_news_link_only_current_rows(
    session: Session,
    domain: str,
    *,
    now: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    link_model = EconomicMarketLink if domain == "economic" else NewsMarketLink
    leg_exists = (
        select(MarketLeg.ticker)
        .where(MarketLeg.ticker == Market.ticker, MarketLeg.category == domain)
        .exists()
    )
    rows = session.execute(
        select(Market, link_model)
        .join(link_model, link_model.ticker == Market.ticker)
        .where(*_market_current_conditions(now), ~leg_exists)
        .order_by(desc(Market.last_seen_at), Market.ticker)
        .limit(limit)
    ).all()
    examples: list[dict[str, Any]] = []
    for row in rows:
        market = row[0]
        link = row[1]
        examples.append(
            {
                "ticker": market.ticker,
                "title": market.title,
                "status": market.status,
                "event_ticker": market.event_ticker,
                "series_ticker": market.series_ticker,
                "close_time": _iso_or_none(market.close_time),
                "expected_expiration_time": _iso_or_none(market.expected_expiration_time),
                "last_seen_at": _iso_or_none(market.last_seen_at),
                "exact_link_present": True,
                "link_reference": _link_reference(domain, link),
                "is_current_market": True,
                "reason_codes": ["CURRENT_EXACT_LINK_WITHOUT_PARSED_LEG"],
            }
        )
    return examples


def _latest_link_rows(
    session: Session,
    domain: str,
    tickers: list[str],
) -> dict[str, EconomicMarketLink | NewsMarketLink]:
    if not tickers:
        return {}
    link_model = EconomicMarketLink if domain == "economic" else NewsMarketLink
    order_column = (
        EconomicMarketLink.detected_at if domain == "economic" else NewsMarketLink.created_at
    )
    links: dict[str, EconomicMarketLink | NewsMarketLink] = {}
    for link in session.scalars(
        select(link_model)
        .where(link_model.ticker.in_(tickers))
        .order_by(desc(order_column), desc(link_model.id))
    ):
        links.setdefault(str(link.ticker), link)
    return links


def _link_reference(
    domain: str,
    link: EconomicMarketLink | NewsMarketLink | None,
) -> dict[str, Any] | None:
    if link is None:
        return None
    if domain == "economic":
        return {
            "event_key": getattr(link, "event_key", None),
            "confidence": getattr(link, "confidence", None),
            "detected_at": _iso_or_none(getattr(link, "detected_at", None)),
        }
    return {
        "news_item_id": getattr(link, "news_item_id", None),
        "confidence": getattr(link, "link_confidence", None),
        "created_at": _iso_or_none(getattr(link, "created_at", None)),
    }


def _is_current_market(market: Market, *, now: datetime) -> tuple[bool, str]:
    status = str(market.status or "").lower()
    close_time = _aware_datetime(market.close_time)
    expected_expiration = _aware_datetime(market.expected_expiration_time)
    if status not in OPEN_MARKET_STATUSES:
        return False, "MARKET_STATUS_NOT_OPEN"
    if close_time is None:
        return False, "MISSING_CLOSE_TIME"
    if close_time <= now:
        return False, "MARKET_CLOSE_TIME_PASSED"
    if expected_expiration is not None and expected_expiration <= now:
        return False, "EXPECTED_EXPIRATION_PASSED"
    if market.settlement_ts is not None or market.result:
        return False, "MARKET_CLOSED_OR_SETTLED"
    return True, "CURRENT_OPEN_MARKET"


def _aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _economic_news_handoff_blocker(
    *,
    current_parsed: int,
    current_linked: int,
    linked_current_without_parse: int,
    non_current: int,
    missing_exact_link: int,
) -> str:
    if current_linked:
        return "READY_FOR_FORECASTS"
    if linked_current_without_parse:
        return "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL"
    if missing_exact_link:
        return "EXACT_LINKS_MISSING"
    if non_current:
        return "ONLY_EXPIRED_OR_CLOSED_PARSED_MARKETS"
    if current_parsed == 0:
        return "NO_CURRENT_PARSED_MARKETS"
    return "WAITING_FOR_COMPATIBLE_MARKETS"


def _economic_news_handoff_next_action(handoff: dict[str, Any]) -> str:
    domains = handoff.get("domains") if isinstance(handoff, dict) else {}
    if not isinstance(domains, dict):
        return (
            "Keep Phase 3BB/domain refresh watches running; do not force links until "
            "compatible parsed markets exist."
        )
    blockers = {
        str(name): _dict(row).get("first_blocker")
        for name, row in domains.items()
        if isinstance(row, dict)
    }
    if "READY_FOR_FORECASTS" in blockers.values():
        return (
            "Run the existing economic/news forecast diagnostics for exact-linked "
            "current markets."
        )
    if "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL" in blockers.values():
        return (
            "Backfill parsed economic/news market legs for exact-linked current "
            "markets; do not use fuzzy links."
        )
    if "EXACT_LINKS_MISSING" in blockers.values():
        return (
            "Run exact ticker link commands only for current parsed economic/news "
            "markets; do not use fuzzy links."
        )
    if "ONLY_EXPIRED_OR_CLOSED_PARSED_MARKETS" in blockers.values():
        return "Keep market refresh running; expired economic/news parsed rows are diagnostic-only."
    return (
        "Keep Phase 3BB/domain refresh watches running; do not force links until "
        "compatible parsed current markets exist."
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _economic_news_blocked_reason(economic: dict[str, Any], news: dict[str, Any]) -> str:
    parts = []
    for label, row in (("economic", economic), ("news", news)):
        status = row.get("status") if isinstance(row, dict) else None
        if status:
            parts.append(f"{label}:{status}")
    return "; ".join(parts) if parts else "No economic/news blocker was reported."


def _general_source_evidence_status(*, reports_dir: Path) -> dict[str, Any]:
    intake = _load_json(reports_dir / "phase3bb_r2_sources" / "general_source_intake.json")
    evidence = _load_json(
        reports_dir / "phase3bb_r2_sources" / "phase3bb_r2_general_source_evidence.json"
    )
    availability = _load_json(
        reports_dir / "phase3bb_r2_sources" / "phase3bb_r2_general_source_availability.json"
    )
    intake_summary = _summary(intake)
    evidence_summary = _summary(evidence)
    availability_summary = _summary(availability)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AM",
        "phase_version": "phase3am_general_source_evidence_status_v1",
        "mode": "REPORT_ONLY_SOURCE_EVIDENCE_STATUS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "summary": {
            "source_intake_present": bool(intake),
            "source_evidence_present": bool(evidence),
            "source_availability_present": bool(availability),
            "template_rows": int(intake_summary.get("template_rows") or 0),
            "diagnostic_rows": int(intake_summary.get("diagnostic_rows") or 0),
            "source_evidence_ready_count": int(
                evidence_summary.get("exact_evidence_ready_rows") or 0
            ),
            "source_value_available_rows": int(
                availability_summary.get("source_value_available_rows") or 0
            ),
            "db_writes": False,
            "link_writes": False,
            "forecast_writes": False,
            "paper_trade_writes": False,
            "settlement_writes": False,
        },
        "intake_summary": intake_summary,
        "evidence_summary": evidence_summary,
        "availability_summary": availability_summary,
        "recommended_next_action": (
            "Fill the grouped source review CSV with operator-verified values; keep "
            "3BB-R2 report-only until evidence is reviewed."
        ),
    }


def _sports_reason_codes(
    placeholder_summary: dict[str, Any],
    evidence_summary: dict[str, Any],
    z_summary: dict[str, Any],
) -> list[str]:
    reasons = []
    if int(placeholder_summary.get("still_placeholder_rows") or 0):
        reasons.append("ROUND_PLACEHOLDERS_UNRESOLVED")
    if int(z_summary.get("rows_safe_to_repair") or 0) == 0:
        reasons.append("NO_SAFE_REPAIR_ROWS")
    if not int(evidence_summary.get("phase3ae_ready_rows") or 0):
        reasons.append("NO_PHASE3AE_READY_ROWS")
    return reasons or ["SPORTS_WATCH_CLEAR_OR_WAITING_FOR_REFRESH"]


def _phase3az_burndown_payload(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    diagnostic: dict[str, Any],
    dry_run: dict[str, Any],
    apply_payload: dict[str, Any] | None,
    general_status: dict[str, Any],
    sports: dict[str, Any],
    economic: dict[str, Any],
) -> dict[str, Any]:
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AM",
        "phase_version": PHASE_3AM_BURNDOWN_VERSION,
        "mode": "PAPER_ONLY_GAP_BURNDOWN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "before_phase3az": _phase3az_counts(before),
        "after_phase3az": _phase3az_counts(after),
        "settlement": {
            "dry_run": dry_run["summary"],
            "apply": apply_payload.get("summary") if isinstance(apply_payload, dict) else None,
            "apply_error": apply_payload.get("error") if isinstance(apply_payload, dict) else None,
            "due_paper_trade_count": diagnostic["summary"]["due_paper_trades"],
            "newly_exact_settled_count": int(
                (apply_payload.get("summary") or {}).get("rows_applied") or 0
            )
            if isinstance(apply_payload, dict)
            else 0,
            "state_counts": diagnostic["summary"]["primary_state_counts"],
        },
        "general_source_evidence": general_status["summary"],
        "sports_gap_watch": sports["summary"],
        "economic_news_watch": economic["summary"],
        "remaining_blockers": _remaining_blockers(after),
        "recommended_next_command": _next_operator_command(
            diagnostic=diagnostic,
            after=after,
            general_status=general_status,
        ),
        "safety_confirmation": {
            "live_trading_enabled": False,
            "demo_exchange_writes_enabled": False,
            "sibling_ticker_settlements_used": 0,
            "fuzzy_title_settlements_used": 0,
        },
    }


def _phase3az_counts(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload, dict) else {}
    gaps = payload.get("gaps") if isinstance(payload, dict) else []
    if not isinstance(summary, dict):
        summary = {}
    if not isinstance(gaps, list):
        gaps = []
    severity_counts = _count_values(gaps, "severity")
    return {
        "gap_count": int(summary.get("gap_count") or len(gaps)),
        "high_gaps": int(summary.get("high_gaps") or severity_counts.get("HIGH", 0)),
        "medium_gaps": int(summary.get("medium_gaps") or severity_counts.get("MEDIUM", 0)),
        "low_gaps": int(summary.get("low_gaps") or severity_counts.get("LOW", 0)),
        "implementation_needed_count": int(summary.get("implementation_needed_count") or 0),
        "top_gap": summary.get("top_gap") or (gaps[0].get("gap_id") if gaps else None),
        "top_phase": summary.get("top_phase"),
    }


def _remaining_blockers(after: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = after.get("gaps") if isinstance(after, dict) else []
    if not isinstance(gaps, list):
        return []
    return [
        {
            "gap_id": row.get("gap_id"),
            "severity": row.get("severity"),
            "phase": row.get("phase"),
            "next_action": row.get("next_action"),
        }
        for row in gaps
        if isinstance(row, dict)
    ]


def _next_operator_command(
    *,
    diagnostic: dict[str, Any],
    after: dict[str, Any],
    general_status: dict[str, Any],
) -> str:
    state_counts = diagnostic["summary"]["primary_state_counts"]
    if state_counts.get("EXACT_SETTLEMENT_READY"):
        return (
            "kalshi-bot phase3ay-settle-due-paper --exact-only --apply "
            "--backup-first --max-records 5 --output-dir reports/phase3am"
        )
    if int(general_status["summary"].get("template_rows") or 0):
        return (
            "kalshi-bot phase3bb-r2-group-source-review "
            "--input reports/phase3bb_r2_sources/phase3bb_r2_general_source_input_template.csv "
            "--output data/general_source_evidence/phase3bb_r2_group_review.csv"
        )
    gaps = after.get("gaps") if isinstance(after, dict) else []
    if isinstance(gaps, list) and gaps:
        command = gaps[0].get("command")
        if command:
            return str(command)
    return "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports"


def _render_burndown_summary(payload: dict[str, Any]) -> str:
    settlement = payload["settlement"]
    states = settlement["state_counts"]
    general = payload["general_source_evidence"]
    sports = payload["sports_gap_watch"]
    economic = payload["economic_news_watch"]
    before = payload["before_phase3az"]
    after = payload["after_phase3az"]
    settled = settlement["newly_exact_settled_count"] > 0
    no_settlement_reason = (
        _not_settled_reason(states)
        if settlement["newly_exact_settled_count"] == 0
        else "Exact rows were applied."
    )
    rejected_sibling_count = states.get("SIBLING_TICKER_REJECTED", 0) + states.get(
        "AMBIGUOUS_MATCH_REJECTED",
        0,
    )
    lines = [
        "# Phase 3AM Executive Summary",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Phase 3AZ gap count: {before['gap_count']} -> {after['gap_count']}",
        (
            "- Implementation-needed count: "
            f"{before['implementation_needed_count']} -> "
            f"{after['implementation_needed_count']}"
        ),
        "",
        "## Answers",
        "",
        f"1. Were any due paper trades safely settled? {settled}.",
        f"2. If not, why not? {no_settlement_reason}",
        f"3. Exact-settlement-ready trades: {states.get('EXACT_SETTLEMENT_READY', 0)}.",
        (
            "4. Waiting for market settlement: "
            f"{states.get('AWAITING_EXACT_MARKET_SETTLEMENT', 0)}."
        ),
        f"5. Composite/local trades: {states.get('COMPOSITE_LOCAL_REQUIRES_RESOLVER', 0)}.",
        f"6. Rejected for sibling/ambiguous tickers: {rejected_sibling_count}.",
        (
            "7. 3BB-R2 source evidence status: "
            f"{general.get('template_rows', 0)} template row(s), "
            f"{general.get('source_evidence_ready_count', 0)} evidence-ready row(s), report-only."
        ),
        (
            "8. Sports blockers: "
            f"{sports.get('unresolved_round_placeholders', 0)} placeholder row(s), "
            f"{sports.get('partial_provenance_sports_markets', 0)} partial-provenance market(s), "
            f"{sports.get('safe_repair_rows', 0)} safe repair row(s)."
        ),
        (
            "9. Economic/news blockers: "
            f"{economic.get('parsed_market_count', 0)} compatible parsed market(s), "
            f"waiting={economic.get('source_data_ready_waiting_for_compatible_markets', False)}."
        ),
        f"10. Next command: `{payload['recommended_next_command']}`.",
        "",
    ]
    return "\n".join(lines)


def _not_settled_reason(states: dict[str, int]) -> str:
    if states.get("EXACT_SETTLEMENT_READY"):
        return "Dry-run only; apply was not requested."
    blockers = [
        f"{state}={count}"
        for state, count in sorted(states.items())
        if state in PHASE3AM_BLOCKED_SETTLEMENT_STATES and count
    ]
    return ", ".join(blockers) if blockers else "No due exact-settlement-ready rows were found."


def _render_phase3am_next_actions(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AM Next Actions",
        "",
        "Run this first:",
        "",
        f"```bash\n{payload['recommended_next_command']}\n```",
        "",
        "Then refresh the gap report:",
        "",
        (
            "```bash\n"
            "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az "
            "--reports-dir reports\n"
            "```"
        ),
        "",
        "Safety reminders:",
        "",
        "- Keep live/demo exchange writes disabled.",
        "- Do not settle from sibling tickers, fuzzy title matches, or component markets.",
        "- Use `--apply --backup-first --exact-only --max-records 5` only after dry-run review.",
        "",
    ]
    return "\n".join(lines)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload, dict) else {}
    return summary if isinstance(summary, dict) else {}


def _count_values(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "UNKNOWN")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _timestamp_for_path() -> str:
    return utc_now().strftime("%Y%m%d_%H%M%S")


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    base = path.parent.resolve()
    for file_path in sorted(files, key=lambda item: item.name):
        if not file_path.exists():
            continue
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        try:
            label = file_path.resolve().relative_to(base).as_posix()
        except ValueError:
            label = file_path.as_posix()
        lines.append(f"{digest}  {label}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
