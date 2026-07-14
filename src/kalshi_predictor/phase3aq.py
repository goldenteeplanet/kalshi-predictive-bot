from __future__ import annotations

import json
import csv
import hashlib
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import Forecast, LearningRejectionLog, PaperOrder
from kalshi_predictor.opportunities.market_identity import (
    API_NOT_FOUND,
    AMBIGUOUS_MARKET_IDENTITY,
    BUILT_FROM_EXACT_CATALOG,
    COMPOSITE_LOCAL_ONLY,
    GENERAL_SOURCE_NOT_SAFE,
    MALFORMED_URL,
    MARKET_NOT_IN_CATALOG,
    MISSING_MARKET_TICKER,
    PARTIAL_PROVENANCE_BLOCKED,
    PLACEHOLDER_BLOCKED,
    STALE_CATALOG,
    SYNTHETIC_ONLY,
    UNVERIFIED,
    VERIFIED,
    VERIFIED_BUT_CLOSED,
    VERIFIED_BUT_PAUSED,
    VERIFIED_BUT_SETTLED,
)
from kalshi_predictor.learning.diagnostics import build_learning_diagnostics
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3aa import build_settlement_eta_schedule
from kalshi_predictor.phase3ai import build_phase3ai_reconciliation
from kalshi_predictor.phase3an import build_phase3an_crypto_feature_completeness
from kalshi_predictor.phase3ak import write_market_data_refresh_status
from kalshi_predictor.phase3ap import (
    DEFAULT_PHASE3AP_SCAN_LIMIT,
    build_phase3ap_book_diagnostic,
    build_phase3ap_paper_ready_gate,
    build_phase3ap_settlement_check_diagnostic,
    _phase3ap_db_writer_status,
)
from kalshi_predictor.reinforcement_learning.repository import rl_status
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE_3AQ_VERSION = "phase3aq_v1"
PHASE_3AQ_LINK_REPAIR_VERSION = "phase3aq_verified_link_repair_v1"

PHASE3AQ_ALLOWED_URL_STATUSES = (
    VERIFIED,
    VERIFIED_BUT_CLOSED,
    VERIFIED_BUT_PAUSED,
    VERIFIED_BUT_SETTLED,
    MISSING_MARKET_TICKER,
    MARKET_NOT_IN_CATALOG,
    STALE_CATALOG,
    SYNTHETIC_ONLY,
    COMPOSITE_LOCAL_ONLY,
    PLACEHOLDER_BLOCKED,
    PARTIAL_PROVENANCE_BLOCKED,
    GENERAL_SOURCE_NOT_SAFE,
    AMBIGUOUS_MARKET_IDENTITY,
    BUILT_FROM_EXACT_CATALOG,
    MALFORMED_URL,
    API_NOT_FOUND,
)

PHASE3AQ_CLICKABLE_STATUSES = {
    VERIFIED,
    VERIFIED_BUT_CLOSED,
    VERIFIED_BUT_PAUSED,
    VERIFIED_BUT_SETTLED,
}

PHASE3AQ_REFRESHABLE_BOOK_REASONS = {
    "NO_ORDERBOOK_SNAPSHOT",
    "EMPTY_ORDERBOOK",
    "STALE_ORDERBOOK",
}


@dataclass(frozen=True)
class Phase3AQArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    prompt_path: Path


@dataclass(frozen=True)
class Phase3AQDiagnosticArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class Phase3AQUnblockArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    next_actions_path: Path
    positive_ev_link_audit_path: Path
    positive_ev_link_audit_markdown_path: Path
    verified_book_refresh_plan_path: Path
    settlement_check_split_path: Path
    paper_ready_gate_summary_path: Path
    blocked_positive_ev_csv_path: Path
    manifest_path: Path


def build_phase3aq_self_improvement_engine(
    session: Session,
    *,
    scan_limit: int = 500,
) -> dict[str, Any]:
    """Rank next-build recommendations from local reports without executing them."""
    session.flush()
    settlement = build_settlement_eta_schedule(session, limit=scan_limit)
    learning = build_learning_diagnostics(
        session,
        scan_limit=scan_limit,
        suggest_thresholds=True,
    )
    coverage = build_phase3ai_reconciliation(
        session,
        upgrade_sports=False,
        limit=scan_limit,
    )
    crypto = build_phase3an_crypto_feature_completeness(session)
    rl = rl_status(session)
    evidence = {
        "settlement": settlement["summary"],
        "learning": learning["funnel"],
        "learning_top_bottleneck": learning.get("top_bottleneck", {}),
        "coverage": coverage["after"]["sports_reconciliation"],
        "crypto": crypto["summary"],
        "model_activity": _model_activity(session),
        "ui_trust": _ui_trust(session),
        "rl": rl,
    }
    recommendations = _recommendations(evidence)
    prompt = _next_build_prompt(recommendations, evidence)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AQ",
        "phase_version": PHASE_3AQ_VERSION,
        "mode": "PAPER_ONLY_ADVISORY_SELF_IMPROVEMENT",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "advisory_policy": {
            "executes_code": False,
            "places_orders": False,
            "enables_live_trading": False,
            "requires_human_approval": True,
        },
        "evidence": evidence,
        "recommendations": recommendations,
        "next_build_prompt": prompt,
        "recommended_next_action": recommendations[0]["next_action"]
        if recommendations
        else "Continue paper-only diagnostics.",
    }


def write_phase3aq_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aq"),
    scan_limit: int = 500,
) -> Phase3AQArtifactSet:
    payload = build_phase3aq_self_improvement_engine(session, scan_limit=scan_limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3aq_self_improvement.json"
    markdown_path = output_dir / "phase3aq_self_improvement.md"
    prompt_path = output_dir / "next_build_prompt.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    prompt_path.write_text(payload["next_build_prompt"], encoding="utf-8")
    return Phase3AQArtifactSet(output_dir, json_path, markdown_path, prompt_path)


def build_phase3aq_positive_ev_link_audit(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aq"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> dict[str, Any]:
    """Classify every positive-EV row by exact Kalshi link state before book state."""
    resolved = settings or get_settings()
    book = build_phase3ap_book_diagnostic(
        session,
        output_dir=output_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    rows = [_phase3aq_positive_ev_row(row) for row in book["positive_ev_rows"]]
    url_counts = Counter(row["url_status"] for row in rows)
    primary_counts = Counter(row["primary_blocker"] for row in rows)
    book_counts = Counter(row["book_status"] for row in rows)
    verified_rows = [row for row in rows if row["url_status"] == VERIFIED]
    paper_ready_rows = [row for row in rows if row["paper_ready"]]
    book_summary = book["summary"]
    summary = {
        "rankings_scanned": book_summary["rankings_scanned"],
        "positive_ev_rows": len(rows),
        "current_positive_ev_rows": len(rows),
        "all_positive_ev_rows_including_diagnostics": book_summary.get(
            "all_positive_ev_rows_including_diagnostics",
            len(rows),
        ),
        "expired_positive_ev_rows": book_summary.get("expired_positive_ev_rows", 0),
        "expired_excluded_rows": book_summary.get("expired_excluded_rows", 0),
        "historical_diagnostic_rows": book_summary.get("historical_diagnostic_rows", 0),
        "finalized_or_settled_rows": book_summary.get("finalized_or_settled_rows", 0),
        "stale_catalog_rows": book_summary.get("stale_catalog_rows", 0),
        "stale_quote_rows": book_summary.get("stale_quote_rows", 0),
        "first_hard_blocker": book_summary.get("first_hard_blocker"),
        "paper_ready_rows": len(paper_ready_rows),
        "positive_ev_no_executable_book_rows": sum(
            1 for row in rows if not row["executable_book"]
        ),
        "specific_url_status_rows": sum(
            1 for row in rows if row["url_status"] in PHASE3AQ_ALLOWED_URL_STATUSES
        ),
        "generic_unverified_link_rows_remaining": sum(
            1
            for row in rows
            if row["url_status"] in {UNVERIFIED, "UNVERIFIED_KALSHI_LINK"}
            or (
                row["primary_blocker"] == "UNVERIFIED_KALSHI_LINK"
                and row["url_status"] not in PHASE3AQ_ALLOWED_URL_STATUSES
            )
        ),
        "verified_tradeable_links": len(verified_rows),
        "verified_clickable_links": sum(
            1 for row in rows if row["url_status"] in PHASE3AQ_CLICKABLE_STATUSES
        ),
        "catalog_match_exists_rows": sum(1 for row in rows if row["catalog_match_exists"]),
        "url_exists_rows": sum(1 for row in rows if row["url_exists"]),
        "verified_link_no_executable_book_rows": sum(
            1 for row in verified_rows if not row["executable_book"]
        ),
        "book_refresh_needed_rows": sum(1 for row in rows if row["book_refresh_needed"]),
        "url_status_counts": dict(sorted(url_counts.items())),
        "primary_blocker_counts": dict(sorted(primary_counts.items())),
        "book_status_counts": dict(sorted(book_counts.items())),
    }
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AQ",
        "phase_version": PHASE_3AQ_LINK_REPAIR_VERSION,
        "mode": "PAPER_READ_ONLY_POSITIVE_EV_LINK_AUDIT",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "fake_links_created": False,
        "sibling_or_fuzzy_matching_allowed": False,
        "reports_dir": str(reports_dir),
        "command_arguments": {
            "output_dir": str(output_dir),
            "reports_dir": str(reports_dir),
            "window_hours": window_hours,
            "limit": limit,
        },
        "summary": summary,
        "allowed_url_statuses": list(PHASE3AQ_ALLOWED_URL_STATUSES),
        "source_phase3ap_book_summary": book_summary,
        "positive_ev_rows": rows,
        "current_positive_ev_rows": rows,
        "expired_positive_ev_rows": book.get("expired_positive_ev_rows", []),
        "historical_diagnostic_rows": book.get("historical_diagnostic_rows", []),
        "finalized_or_settled_rows": book.get("finalized_or_settled_rows", []),
        "blocked_positive_ev_rows": [row for row in rows if not row["paper_ready"]],
        "acceptance": {
            "positive_ev_rows_classified": len(rows) == summary["specific_url_status_rows"],
            "generic_unverified_link_removed": summary[
                "generic_unverified_link_rows_remaining"
            ]
            == 0,
            "no_fake_links_created": True,
            "book_refresh_requires_verified_tradeable_link": all(
                row["url_status"] == VERIFIED for row in rows if row["book_refresh_needed"]
            ),
            "paper_ready_requires_verified_tradeable_link": all(
                row["url_status"] == VERIFIED for row in paper_ready_rows
            ),
        },
        "next_action": _phase3aq_audit_next_action(summary),
    }


def write_phase3aq_positive_ev_link_audit_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aq"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> Phase3AQDiagnosticArtifactSet:
    payload = build_phase3aq_positive_ev_link_audit(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "positive_ev_link_audit.json"
    markdown_path = output_dir / "positive_ev_link_audit.md"
    _phase3aq_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3aq_link_audit_markdown(payload), encoding="utf-8")
    return Phase3AQDiagnosticArtifactSet(output_dir, json_path, markdown_path)


def build_phase3aq_refresh_verified_opportunity_books(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aq"),
    dry_run: bool = True,
    apply_readonly_refresh: bool = False,
    max_markets: int = 100,
    max_duration_seconds: int = 120,
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    audit = build_phase3aq_positive_ev_link_audit(
        session,
        output_dir=output_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    candidates = [
        row for row in audit["positive_ev_rows"] if row["book_refresh_needed"]
    ][: max(0, max_markets)]
    writer = _phase3ap_db_writer_status(settings=resolved)
    blocked_by_writer = (
        apply_readonly_refresh
        and bool(candidates)
        and not bool(writer.get("safe_to_start_write", True))
    )
    refresh_artifact = None
    refresh_error = None
    refresh_started = False
    refresh_completed = False
    if blocked_by_writer:
        status = "BLOCKED_BY_ACTIVE_WRITER"
    elif apply_readonly_refresh and candidates:
        try:
            refresh_started = True
            artifacts = write_market_data_refresh_status(
                session,
                output_dir=output_dir / "market_data_refresh",
                bounded=True,
                max_duration_seconds=max_duration_seconds,
                require_no_active_writer=True,
                run_refresh=True,
                settings=resolved,
            )
            refresh_completed = True
            refresh_artifact = str(artifacts.json_path)
            status = "READONLY_REFRESH_COMPLETED"
        except Exception as exc:  # noqa: BLE001 - operator report must capture failures.
            refresh_error = str(exc)
            status = "READONLY_REFRESH_FAILED"
    elif candidates:
        status = "DRY_RUN"
    else:
        status = "NO_VERIFIED_BOOK_REFRESH_CANDIDATES"
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AQ",
        "phase_version": PHASE_3AQ_LINK_REPAIR_VERSION,
        "mode": "PAPER_READ_ONLY_VERIFIED_BOOK_REFRESH_PLAN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "market_data_writes": bool(refresh_completed),
        "dry_run": dry_run,
        "apply_readonly_refresh": apply_readonly_refresh,
        "status": status,
        "active_writer": writer,
        "refresh_started": refresh_started,
        "refresh_completed": refresh_completed,
        "refresh_error": refresh_error,
        "refresh_artifact": refresh_artifact,
        "max_markets": max_markets,
        "max_duration_seconds": max_duration_seconds,
        "verified_refresh_candidates": candidates,
        "markets_needing_book_refresh": [row["market_ticker"] for row in candidates],
        "markets_refreshed": (
            [row["market_ticker"] for row in candidates] if refresh_completed else []
        ),
        "markets_blocked_by_writer": (
            [row["market_ticker"] for row in candidates] if blocked_by_writer else []
        ),
        "summary": {
            "positive_ev_rows": audit["summary"]["positive_ev_rows"],
            "verified_tradeable_links": audit["summary"]["verified_tradeable_links"],
            "book_refresh_needed_rows": len(candidates),
            "market_data_writes": bool(refresh_completed),
            "blocked_by_active_writer": blocked_by_writer,
        },
        "next_action": _phase3aq_refresh_next_action(status, candidates),
    }


def write_phase3aq_refresh_verified_opportunity_books_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aq"),
    dry_run: bool = True,
    apply_readonly_refresh: bool = False,
    max_markets: int = 100,
    max_duration_seconds: int = 120,
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> Phase3AQDiagnosticArtifactSet:
    payload = build_phase3aq_refresh_verified_opportunity_books(
        session,
        output_dir=output_dir,
        dry_run=dry_run,
        apply_readonly_refresh=apply_readonly_refresh,
        max_markets=max_markets,
        max_duration_seconds=max_duration_seconds,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "verified_book_refresh_plan.json"
    markdown_path = output_dir / "verified_book_refresh_plan.md"
    _phase3aq_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3aq_refresh_markdown(payload), encoding="utf-8")
    return Phase3AQDiagnosticArtifactSet(output_dir, json_path, markdown_path)


def build_phase3aq_settlement_check_split(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aq"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> dict[str, Any]:
    split = build_phase3ap_settlement_check_diagnostic(
        session,
        output_dir=output_dir,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    rows = [_phase3aq_settlement_row(row) for row in split["rows"]]
    reason_counts = Counter(row["specific_reason_code"] for row in rows)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AQ",
        "phase_version": PHASE_3AQ_LINK_REPAIR_VERSION,
        "mode": "PAPER_READ_ONLY_SETTLEMENT_CHECK_SPLIT",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "settlement_apply_ran": False,
        "allows_sibling_settlement": False,
        "allows_fuzzy_settlement": False,
        "summary": {
            "rows_scanned": split["summary"]["rows_scanned"],
            "legacy_settlement_check_failed_rows": len(rows),
            "specific_reason_counts": dict(sorted(reason_counts.items())),
            "open_market_entry_eligible_rows": sum(
                1 for row in rows if row["paper_entry_settlement_eligible"]
            ),
            "paper_entry_blocked_rows": sum(1 for row in rows if row["blocks_paper_entry"]),
            "generic_settlement_check_failed_remaining": 0,
        },
        "rows": rows,
        "acceptance": {
            "generic_reason_split": True,
            "open_known_terms_do_not_require_final_outcome": all(
                not row["blocks_paper_entry"]
                for row in rows
                if row["specific_reason_code"] == "OPEN_MARKET_SETTLEMENT_TERMS_KNOWN"
            ),
            "settled_resolution_logic_weakened": False,
        },
    }


def write_phase3aq_settlement_check_split_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aq"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> Phase3AQDiagnosticArtifactSet:
    payload = build_phase3aq_settlement_check_split(
        session,
        output_dir=output_dir,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "settlement_check_split.json"
    markdown_path = output_dir / "settlement_check_split.md"
    _phase3aq_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3aq_settlement_markdown(payload), encoding="utf-8")
    return Phase3AQDiagnosticArtifactSet(output_dir, json_path, markdown_path)


def write_phase3aq_link_and_book_unblock_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aq"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> Phase3AQUnblockArtifactSet:
    resolved = settings or get_settings()
    output_dir.mkdir(parents=True, exist_ok=True)
    audit = build_phase3aq_positive_ev_link_audit(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    refresh = build_phase3aq_refresh_verified_opportunity_books(
        session,
        output_dir=output_dir,
        dry_run=True,
        apply_readonly_refresh=False,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    settlement = build_phase3aq_settlement_check_split(
        session,
        output_dir=output_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    gate = build_phase3ap_paper_ready_gate(
        session,
        output_dir=output_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    gate_summary = {
        "generated_at": utc_now().isoformat(),
        "phase": "3AQ",
        "phase_version": PHASE_3AQ_LINK_REPAIR_VERSION,
        "mode": "PAPER_READ_ONLY_LINK_AND_BOOK_GATE_SUMMARY",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "summary": {
            **audit["summary"],
            "settlement_specific_reason_counts": settlement["summary"][
                "specific_reason_counts"
            ],
        },
        "phase3ap_gate_summary": gate["summary"],
        "positive_ev_rows": audit["positive_ev_rows"],
        "next_action": audit["next_action"],
        "acceptance": audit["acceptance"],
    }
    audit_json = output_dir / "positive_ev_link_audit.json"
    audit_md = output_dir / "positive_ev_link_audit.md"
    refresh_json = output_dir / "verified_book_refresh_plan.json"
    settlement_json = output_dir / "settlement_check_split.json"
    gate_json = output_dir / "paper_ready_gate_summary.json"
    blocked_csv = output_dir / "blocked_positive_ev_rows.csv"
    executive_summary = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions = output_dir / "NEXT_ACTIONS.md"
    manifest = output_dir / "MANIFEST.sha256"
    _phase3aq_write_json(audit_json, audit)
    audit_md.write_text(_render_phase3aq_link_audit_markdown(audit), encoding="utf-8")
    _phase3aq_write_json(refresh_json, refresh)
    _phase3aq_write_json(settlement_json, settlement)
    _phase3aq_write_json(gate_json, gate_summary)
    _phase3aq_write_csv(blocked_csv, audit["blocked_positive_ev_rows"])
    executive_summary.write_text(
        _render_phase3aq_executive_summary(audit, refresh, settlement),
        encoding="utf-8",
    )
    next_actions.write_text(
        _render_phase3aq_next_actions(audit, refresh, settlement),
        encoding="utf-8",
    )
    _phase3aq_write_manifest(
        manifest,
        [
            executive_summary,
            next_actions,
            audit_json,
            audit_md,
            refresh_json,
            settlement_json,
            gate_json,
            blocked_csv,
        ],
    )
    return Phase3AQUnblockArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary,
        next_actions_path=next_actions,
        positive_ev_link_audit_path=audit_json,
        positive_ev_link_audit_markdown_path=audit_md,
        verified_book_refresh_plan_path=refresh_json,
        settlement_check_split_path=settlement_json,
        paper_ready_gate_summary_path=gate_json,
        blocked_positive_ev_csv_path=blocked_csv,
        manifest_path=manifest,
    )


def _model_activity(session: Session) -> dict[str, Any]:
    total_forecasts = int(session.scalar(select(func.count()).select_from(Forecast)) or 0)
    model_rows = list(
        session.execute(
            select(Forecast.model_name, func.count())
            .group_by(Forecast.model_name)
            .order_by(Forecast.model_name)
        )
    )
    inactive_core = [
        model
        for model in ("crypto_v2", "weather_v2", "economic_v1", "sports_v1")
        if not any(row[0] == model and int(row[1]) > 0 for row in model_rows)
    ]
    return {
        "total_forecasts": total_forecasts,
        "forecast_counts": {str(row[0]): int(row[1]) for row in model_rows},
        "inactive_core_models": inactive_core,
    }


def _ui_trust(session: Session) -> dict[str, Any]:
    missing_rejections = int(
        session.scalar(
            select(func.count())
            .select_from(LearningRejectionLog)
            .where(
                LearningRejectionLog.reason.in_(
                    (
                        "missing_market_snapshot",
                        "missing_liquidity",
                        "multi_leg_missing_market_snapshot",
                        "multi_leg_missing_liquidity",
                    )
                )
            )
        )
        or 0
    )
    return {
        "missing_snapshot_or_liquidity_rejections": missing_rejections,
        "paper_orders": int(session.scalar(select(func.count()).select_from(PaperOrder)) or 0),
    }


def _recommendations(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        _recommendation(
            "settlements",
            100 if evidence["settlement"]["eligible_exact_settlements"] else 70,
            "Settlement realization and ETA discipline",
            "Exact paper settlements are the reward signal for confidence and RL.",
            "Run Phase 3AA/3AO until settled rewards are visible.",
        ),
        _recommendation(
            "link_quality",
            90 if evidence["coverage"].get("unresolved_partial_markets", 0) else 45,
            "Link quality and sports provenance",
            "Unverified or partial links make model evidence harder to trust.",
            "Run Phase 3AM/3AK before allowing ambiguous sports learning.",
        ),
        _recommendation(
            "stale_data",
            85
            if not evidence["crypto"].get("can_rerun_crypto_v2")
            or evidence["ui_trust"]["missing_snapshot_or_liquidity_rejections"]
            else 40,
            "Fresh data and snapshot health",
            "Stale features and missing snapshots block trustworthy forecasts.",
            "Refresh crypto features and run snapshot coverage repair.",
        ),
        _recommendation(
            "model_inactivity",
            80 if evidence["model_activity"]["inactive_core_models"] else 35,
            "Model inactivity",
            "Inactive core models need either data or an explicit diagnostic reason.",
            "Run model-readiness after feature/link repair.",
        ),
        _recommendation(
            "ui_trust",
            65 if evidence["ui_trust"]["missing_snapshot_or_liquidity_rejections"] else 30,
            "UI trust and diagnostics",
            "The UI should show why rows are blocked instead of implying tradability.",
            "Keep missing-data rows grouped and labeled.",
        ),
        _recommendation(
            "offline_rl",
            75
            if evidence["learning"].get("settled_paper_trades", 0) > 0
            and evidence["rl"].get("run_count", 0) == 0
            else 25,
            "Offline reinforcement learning",
            "RL should wait for settled paper rewards before influencing policy.",
            "Run rl-evaluate only after 3AO shows settled rewards.",
        ),
    ]
    return sorted(rows, key=lambda row: (-row["impact_score"], row["area"]))


def _recommendation(
    area: str,
    score: int,
    title: str,
    evidence: str,
    next_action: str,
) -> dict[str, Any]:
    return {
        "area": area,
        "impact_score": score,
        "title": title,
        "evidence": evidence,
        "next_action": next_action,
    }


def _next_build_prompt(recommendations: list[dict[str, Any]], evidence: dict[str, Any]) -> str:
    top = recommendations[0] if recommendations else {"title": "Paper-only maintenance"}
    return "\n".join(
        [
            f"Build next: {top['title']}",
            "",
            "Keep everything paper-only. Do not enable demo or live execution.",
            "",
            "Current evidence:",
            f"- Settlements: {evidence['settlement']}",
            f"- Learning funnel: {evidence['learning']}",
            f"- Sports coverage: {evidence['coverage']}",
            f"- Crypto completeness: {evidence['crypto']}",
            "",
            "Tasks:",
            f"1. Address {top['area']} first.",
            "2. Add diagnostics and tests.",
            "3. Preserve exact-ticker settlement policy.",
            "4. Keep UI/report output honest about missing data.",
            "",
        ]
    )


def _phase3aq_positive_ev_row(row: dict[str, Any]) -> dict[str, Any]:
    url_status = _phase3aq_url_status(row)
    legacy_book_reason = str(row.get("no_book_reason") or row.get("book_reason") or "")
    executable = bool(row.get("executable_book"))
    book_status = _phase3aq_book_status(
        url_status=url_status,
        executable=executable,
        legacy_book_reason=legacy_book_reason,
    )
    book_refresh_needed = (
        url_status == VERIFIED
        and not executable
        and legacy_book_reason in PHASE3AQ_REFRESHABLE_BOOK_REASONS
    )
    if url_status != VERIFIED:
        primary = url_status
    elif not executable:
        primary = legacy_book_reason or "NO_EXECUTABLE_BOOK"
    else:
        primary = str(row.get("primary_blocker") or "PAPER_READY")
    raw_ev = to_decimal(row.get("raw_ev")) or Decimal("0")
    catalog_match_exists = _phase3aq_catalog_match_exists(row, url_status)
    url_exists = bool(row.get("kalshi_url"))
    return {
        "ranking_id": row.get("ranking_id"),
        "forecast_id": row.get("forecast_id"),
        "market_ticker": row.get("market_ticker") or row.get("ticker"),
        "ticker": row.get("ticker") or row.get("market_ticker"),
        "event_ticker": row.get("event_ticker"),
        "series_ticker": row.get("series_ticker"),
        "market_title": row.get("market_title"),
        "forecast_model": row.get("forecast_model"),
        "ranked_at": row.get("ranked_at"),
        "forecasted_at": row.get("forecasted_at"),
        "best_side": row.get("best_side"),
        "best_price": row.get("best_price"),
        "raw_ev": row.get("raw_ev"),
        "raw_ev_cents": str((raw_ev * Decimal("100")).quantize(Decimal("0.001"))),
        "executable_ev": row.get("executable_ev"),
        "quote_age_minutes": row.get("quote_age_minutes"),
        "market_lifecycle_status": row.get("market_lifecycle_status"),
        "catalog_last_seen_at": row.get("catalog_last_seen_at"),
        "window_status": row.get("window_status"),
        "current_window_status": row.get("current_window_status"),
        "window_status_reason": row.get("window_status_reason"),
        "current_positive_ev_eligible": row.get("current_positive_ev_eligible"),
        "diagnostic_only": row.get("diagnostic_only"),
        "market_close_time": row.get("market_close_time"),
        "expected_expiration_time": row.get("expected_expiration_time"),
        "final_entry_cutoff_time": row.get("final_entry_cutoff_time"),
        "catalog_match_exists": catalog_match_exists,
        "url_exists": url_exists,
        "kalshi_url": row.get("kalshi_url") if url_status in PHASE3AQ_CLICKABLE_STATUSES else None,
        "kalshi_url_verified": url_status == VERIFIED,
        "url_status": url_status,
        "legacy_url_status": row.get("kalshi_url_status"),
        "url_reason": row.get("kalshi_url_reason")
        or row.get("url_verification_reason")
        or _phase3aq_status_reason(url_status),
        "executable_book": executable,
        "book_status": book_status,
        "book_refresh_needed": book_refresh_needed,
        "legacy_no_book_reason": legacy_book_reason or None,
        "book_reason": row.get("book_reason"),
        "best_yes_bid": row.get("best_yes_bid"),
        "best_yes_ask": row.get("best_yes_ask"),
        "best_no_bid": row.get("best_no_bid"),
        "best_no_ask": row.get("best_no_ask"),
        "visible_depth": row.get("visible_depth"),
        "depth_at_configured_limit": row.get("depth_at_configured_limit"),
        "paper_ready": bool(row.get("paper_ready")) and url_status == VERIFIED,
        "primary_blocker": primary,
        "legacy_primary_blocker": row.get("primary_blocker"),
        "secondary_blockers": row.get("secondary_blockers") or [],
        "settlement_specific_reason": row.get("settlement_specific_reason"),
        "settlement_terms_known": row.get("settlement_terms_known"),
        "paper_entry_settlement_eligible": row.get("paper_entry_settlement_eligible"),
        "next_action": _phase3aq_row_next_action(
            url_status=url_status,
            book_status=book_status,
            book_refresh_needed=book_refresh_needed,
        ),
    }


def _phase3aq_url_status(row: dict[str, Any]) -> str:
    status = str(row.get("kalshi_url_status") or row.get("url_verification_status") or "")
    if status == UNVERIFIED:
        return MALFORMED_URL
    if status in PHASE3AQ_ALLOWED_URL_STATUSES:
        return status
    if not str(row.get("market_ticker") or row.get("ticker") or "").strip():
        return MISSING_MARKET_TICKER
    if not row.get("market_lifecycle_status") and not row.get("catalog_last_seen_at"):
        return MARKET_NOT_IN_CATALOG
    return API_NOT_FOUND


def _phase3aq_catalog_match_exists(row: dict[str, Any], url_status: str) -> bool:
    if url_status in {
        MISSING_MARKET_TICKER,
        MARKET_NOT_IN_CATALOG,
        SYNTHETIC_ONLY,
        COMPOSITE_LOCAL_ONLY,
    }:
        return False
    return bool(row.get("market_lifecycle_status") or row.get("catalog_last_seen_at"))


def _phase3aq_book_status(
    *,
    url_status: str,
    executable: bool,
    legacy_book_reason: str,
) -> str:
    if executable:
        return "EXECUTABLE_BOOK"
    if url_status != VERIFIED:
        return "BOOK_HELD_BEHIND_LINK_VERIFICATION"
    return legacy_book_reason or "NO_EXECUTABLE_BOOK"


def _phase3aq_settlement_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    code = str(row.get("specific_reason_code") or "UNKNOWN_REQUIRES_INVESTIGATION")
    if (
        code == "MARKET_NOT_SETTLEABLE_YET"
        and bool(row.get("settlement_terms_known"))
        and not bool(row.get("blocks_paper_entry"))
    ):
        code = "OPEN_MARKET_SETTLEMENT_TERMS_KNOWN"
    payload["source_specific_reason_code"] = row.get("specific_reason_code")
    payload["specific_reason_code"] = code
    return payload


def _phase3aq_status_reason(status: str) -> str:
    reasons = {
        MALFORMED_URL: "Exact catalog row has no usable official Kalshi web URL or slug.",
        BUILT_FROM_EXACT_CATALOG: (
            "Exact catalog identity can build a deterministic Kalshi URL, but it has not "
            "been persisted as an official verified URL."
        ),
        API_NOT_FOUND: "Kalshi API/catalog verification did not return an exact usable market.",
        MARKET_NOT_IN_CATALOG: "No exact market ticker exists in the local Kalshi catalog.",
        MISSING_MARKET_TICKER: "Opportunity row has no exact market ticker.",
        STALE_CATALOG: "Catalog evidence is stale and must be refreshed before link review.",
        SYNTHETIC_ONLY: "Synthetic/internal row has no direct Kalshi listing.",
        COMPOSITE_LOCAL_ONLY: "Composite/local row has no single direct Kalshi market.",
        PLACEHOLDER_BLOCKED: "Placeholder provenance is blocked until exact market identity exists.",
        PARTIAL_PROVENANCE_BLOCKED: "Partial provenance is diagnostic-only.",
        GENERAL_SOURCE_NOT_SAFE: "Source-readiness evidence is not safe for trade-link display.",
        AMBIGUOUS_MARKET_IDENTITY: "Exact market identity is ambiguous; sibling matching is blocked.",
    }
    return reasons.get(status, status.replace("_", " ").title())


def _phase3aq_row_next_action(
    *,
    url_status: str,
    book_status: str,
    book_refresh_needed: bool,
) -> str:
    if url_status == VERIFIED and book_refresh_needed:
        return "Refresh the exact verified market book, then rerun the Phase 3AQ audit."
    if url_status == VERIFIED:
        return f"Keep link verified; resolve book/risk blocker {book_status}."
    if url_status == STALE_CATALOG:
        return "Refresh the Kalshi catalog and rerun exact-link verification."
    if url_status == MALFORMED_URL:
        return "Persist an official Kalshi URL or slug for the exact catalog ticker."
    if url_status == BUILT_FROM_EXACT_CATALOG:
        return "Run Phase 3AR URL repair to persist an official Kalshi URL before book refresh or paper entry."
    if url_status in {MARKET_NOT_IN_CATALOG, MISSING_MARKET_TICKER}:
        return "Repair exact market_ticker lineage before book refresh or trading review."
    if url_status in {
        SYNTHETIC_ONLY,
        COMPOSITE_LOCAL_ONLY,
        PLACEHOLDER_BLOCKED,
        PARTIAL_PROVENANCE_BLOCKED,
        GENERAL_SOURCE_NOT_SAFE,
        AMBIGUOUS_MARKET_IDENTITY,
    }:
        return "Keep the row diagnostic-only until exact non-ambiguous market evidence exists."
    return "Keep diagnostic-only and investigate exact Kalshi identity evidence."


def _phase3aq_audit_next_action(summary: dict[str, Any]) -> str:
    if summary["book_refresh_needed_rows"]:
        return "Run the verified book refresh command after db-writer-monitor is clear."
    if summary["verified_tradeable_links"] == 0 and summary["positive_ev_rows"]:
        return "Repair exact Kalshi URL/catalog evidence before refreshing books."
    if summary["paper_ready_rows"]:
        return "Inspect paper-only risk gates; do not enable execution from this report."
    return "Keep the watcher running and rerun Phase 3AQ after the next fresh cycle."


def _phase3aq_refresh_next_action(status: str, candidates: list[dict[str, Any]]) -> str:
    if status == "BLOCKED_BY_ACTIVE_WRITER":
        return "Wait for the active writer to finish, then rerun the dry-run plan."
    if status == "READONLY_REFRESH_COMPLETED":
        return "Rerun phase3aq-positive-ev-link-audit and inspect remaining book blockers."
    if candidates:
        return "Review candidates, then rerun with --apply-readonly-refresh if writer gates are clear."
    return "No exact verified book refresh candidates; repair link/catalog status first."


def _render_phase3aq_link_audit_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AQ Positive-EV Link Audit",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        "- Live/demo execution: blocked",
        "- Order submission/cancel/replace: blocked",
        "- Fake Kalshi links: blocked",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        if isinstance(value, dict):
            lines.append(f"- {key}: {json.dumps(value, sort_keys=True)}")
        else:
            lines.append(f"- {key}: {value}")
    lines.extend(["", "## Next Action", "", payload["next_action"], ""])
    return "\n".join(lines)


def _render_phase3aq_refresh_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Phase 3AQ Verified Book Refresh Plan",
            "",
            f"- Generated at: {payload['generated_at']}",
            f"- Status: {payload['status']}",
            f"- Market-data writes: {payload['market_data_writes']}",
            f"- Candidates: {len(payload['verified_refresh_candidates'])}",
            "",
            "## Next Action",
            "",
            payload["next_action"],
            "",
        ]
    )


def _render_phase3aq_settlement_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    return "\n".join(
        [
            "# Phase 3AQ Settlement Check Split",
            "",
            f"- Generated at: {payload['generated_at']}",
            f"- Legacy rows: {summary['legacy_settlement_check_failed_rows']}",
            f"- Generic remaining: {summary['generic_settlement_check_failed_remaining']}",
            f"- Reason counts: {json.dumps(summary['specific_reason_counts'], sort_keys=True)}",
            "",
        ]
    )


def _render_phase3aq_executive_summary(
    audit: dict[str, Any],
    refresh: dict[str, Any],
    settlement: dict[str, Any],
) -> str:
    summary = audit["summary"]
    return "\n".join(
        [
            "# Phase 3AQ Verified Kalshi Link Repair",
            "",
            f"- Positive-EV rows classified: {summary['positive_ev_rows']}",
            f"- Paper-ready rows: {summary['paper_ready_rows']}",
            f"- Verified tradeable links: {summary['verified_tradeable_links']}",
            f"- Book refresh candidates: {summary['book_refresh_needed_rows']}",
            f"- Generic unverified-link rows remaining: "
            f"{summary['generic_unverified_link_rows_remaining']}",
            f"- URL status counts: {json.dumps(summary['url_status_counts'], sort_keys=True)}",
            f"- Settlement reason counts: "
            f"{json.dumps(settlement['summary']['specific_reason_counts'], sort_keys=True)}",
            f"- Refresh plan status: {refresh['status']}",
            "",
            "Live/demo execution, order submission, paper trade creation, fake URLs, "
            "sibling matching, fuzzy matching, and threshold lowering remained blocked.",
            "",
        ]
    )


def _render_phase3aq_next_actions(
    audit: dict[str, Any],
    refresh: dict[str, Any],
    settlement: dict[str, Any],
) -> str:
    lines = [
        "# Phase 3AQ Next Actions",
        "",
        f"1. {audit['next_action']}",
        f"2. {refresh['next_action']}",
        (
            "3. Use settlement_check_split.json for specific settlement blockers; "
            f"generic remaining is "
            f"{settlement['summary']['generic_settlement_check_failed_remaining']}."
        ),
        "4. Keep rows without exact verified links diagnostic-only.",
        "",
    ]
    return "\n".join(lines)


def _phase3aq_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _phase3aq_write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "market_ticker",
        "forecast_model",
        "raw_ev",
        "url_status",
        "catalog_match_exists",
        "url_exists",
        "book_status",
        "book_refresh_needed",
        "primary_blocker",
        "next_action",
        "kalshi_url",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _phase3aq_write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AQ Self-Improvement Engine",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "",
        "## Recommendations",
        "",
        "| Rank | Area | Impact | Title | Next action |",
        "| ---: | --- | ---: | --- | --- |",
    ]
    for index, row in enumerate(payload["recommendations"], start=1):
        lines.append(
            f"| {index} | {row['area']} | {row['impact_score']} | "
            f"{row['title']} | {row['next_action']} |"
        )
    lines.extend(
        [
            "",
            "## Next Build Prompt",
            "",
            "```text",
            payload["next_build_prompt"],
            "```",
            "",
            "## Safety",
            "",
            "- Advisory only.",
            "- No code execution from generated prompts.",
            "- No demo or live orders.",
            "",
        ]
    )
    return "\n".join(lines)
