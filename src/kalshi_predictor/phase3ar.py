from __future__ import annotations

import json
import csv
import hashlib
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import (
    is_active_market_status,
    is_inactive_market_status,
    is_link_deprecated,
    latest_links_for_table,
)
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.repository import get_latest_crypto_features
from kalshi_predictor.data.backend import database_url_from_settings, redact_database_url
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.crypto.semantics import (
    EXACT_LINK,
    CryptoMarketTerms,
    select_compatible_crypto_feature,
    terms_from_link_payload,
)
from kalshi_predictor.data.repositories import decode_json, encode_json, insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    Forecast,
    ForecastSkipLog,
    Market,
    MarketRanking,
    MarketSnapshot,
)
from kalshi_predictor.kalshi.client import (
    RATE_LIMITED_ABORTED,
    RATE_LIMITED_PARTIAL,
    RATE_LIMITED_RETRY_EXHAUSTED,
    KalshiAPIError,
    KalshiClient,
    KalshiRetryError,
)
from kalshi_predictor.opportunities.market_identity import (
    BUILT_FROM_EXACT_CATALOG,
    CATALOG_MATCH_AMBIGUOUS,
    CATALOG_MATCH_MISSING,
    CATALOG_STALE,
    COMPOSITE_LOCAL_ONLY,
    GENERAL_SOURCE_NOT_SAFE,
    MALFORMED_URL,
    MISSING_MARKET_TICKER,
    PARTIAL_PROVENANCE_BLOCKED,
    PLACEHOLDER_BLOCKED,
    STALE_CATALOG,
    SYNTHETIC_ONLY,
    TICKER_MISMATCH,
    VERIFIED,
    build_canonical_kalshi_url,
)
from kalshi_predictor.opportunities.window_eligibility import (
    EXPIRED_WINDOW_EXCLUDED,
    MARKET_CLOSED_OR_SETTLED,
    current_market_window_status,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.decimals import midpoint, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE_3AR_VERSION = "phase3ar_v1"
PHASE_3AR_LINK_REPAIR_VERSION = "phase3ar_link_repair_v1"
DEFAULT_PHASE3AR_EFFECTIVE_SCAN_LIMIT = 500

URL_REPAIR_REASON_CODES = (
    "URL_MISSING",
    "URL_EMPTY",
    "URL_NOT_HTTP",
    "URL_BAD_DOMAIN",
    "URL_MISSING_MARKET_TICKER",
    "URL_TICKER_MISMATCH",
    "URL_USES_EVENT_NOT_MARKET",
    "URL_USES_SERIES_NOT_MARKET",
    "URL_SLUG_MISSING",
    "URL_SLUG_MISMATCH",
    "URL_HAS_INTERNAL_ID",
    "URL_HAS_SYNTHETIC_ID",
    "URL_HAS_COMPOSITE_ID",
    "URL_PARSE_FAILED",
    "CATALOG_MATCH_MISSING",
    "CATALOG_MATCH_AMBIGUOUS",
    "CATALOG_STALE",
    "STALE_CATALOG",
    "UNKNOWN_REQUIRES_INVESTIGATION",
)

CATALOG_STALE_REASON_CODES = (
    "CATALOG_LAST_SEEN_TOO_OLD",
    "ACTIVE_MARKET_REFRESH_NOT_RUN",
    "MARKET_MISSING_FROM_ACTIVE_REFRESH",
    "EVENT_METADATA_STALE",
    "SERIES_METADATA_STALE",
    "SLUG_OR_TITLE_MISSING",
    "LIFECYCLE_UNKNOWN",
    "MARKET_CLOSED_OR_SETTLED",
    "DATABASE_MISMATCH",
    "UNKNOWN_REQUIRES_INVESTIGATION",
)

PHASE3AR_RATE_LIMIT_STATUSES = {
    RATE_LIMITED_PARTIAL,
    RATE_LIMITED_ABORTED,
    RATE_LIMITED_RETRY_EXHAUSTED,
}

STATUS_READY = "READY_TO_FORECAST"
STATUS_NO_SNAPSHOT = "NO_LINKED_SNAPSHOT"
STATUS_LOW_CONFIDENCE = "LOW_LINK_CONFIDENCE"
STATUS_AMBIGUOUS_TERMS = "AMBIGUOUS_OR_UNSUPPORTED_TERMS"
STATUS_FUTURE_FEATURE = "FUTURE_FEATURE_BLOCK"
STATUS_STALE_FEATURE = "STALE_FEATURE_BLOCK"
STATUS_MISSING_FEATURE = "MISSING_COMPONENT_FEATURE"
STATUS_INSUFFICIENT_HISTORY = "INSUFFICIENT_FEATURE_HISTORY"
STATUS_NO_MOMENTUM = "NO_CRYPTO_MOMENTUM"
STATUS_NO_MIDPOINT = "NO_MARKET_MIDPOINT"
STATUS_NO_MARKET = "NO_CATALOG_MARKET"
STATUS_CLOSED_MARKET = "CLOSED_OR_INACTIVE_MARKET"
STATUS_EXPIRED_WINDOW_EXCLUDED = EXPIRED_WINDOW_EXCLUDED
STATUS_STALE_QUOTE = "STALE_QUOTE"
QUOTE_STALE_AFTER_MINUTES = Decimal("15")


class CryptoSnapshotClient(Protocol):
    def get_market(self, ticker: str) -> Mapping[str, Any]:
        ...

    def get_orderbook(self, ticker: str) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class Phase3ARArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


@dataclass(frozen=True)
class Phase3ARLinkArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class Phase3ARLinkRepairReportSet:
    output_dir: Path
    executive_summary_path: Path
    next_actions_path: Path
    url_audit_path: Path
    url_audit_markdown_path: Path
    catalog_stale_diagnostic_path: Path
    catalog_stale_diagnostic_markdown_path: Path
    catalog_refresh_plan_path: Path
    catalog_refresh_plan_markdown_path: Path
    url_repair_dry_run_path: Path
    book_refresh_plan_path: Path
    book_refresh_candidates_path: Path
    paper_ready_gate_path: Path
    blocked_positive_ev_csv_path: Path
    manifest_path: Path


def build_crypto_forecast_coverage(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int = 500,
    repair_snapshots: bool = False,
    client: CryptoSnapshotClient | None = None,
    tickers: list[str] | None = None,
) -> dict[str, Any]:
    """Diagnose why linked crypto markets do or do not produce crypto_v2 forecasts."""
    resolved = settings or get_settings()
    session.flush()
    ticker_scope = _unique_tickers(tickers or [])
    effective_limit = (
        max(0, int(limit))
        if ticker_scope
        else min(max(0, int(limit)), DEFAULT_PHASE3AR_EFFECTIVE_SCAN_LIMIT)
    )
    before_rows = _diagnostic_rows(
        session,
        settings=resolved,
        limit=effective_limit,
        tickers=ticker_scope or None,
    )
    repair_result = (
        repair_crypto_linked_snapshots(
            session,
            settings=resolved,
            limit=effective_limit,
            client=client,
            candidate_rows=before_rows,
        )
        if repair_snapshots
        else _empty_repair_result()
    )
    rows = _diagnostic_rows(
        session,
        settings=resolved,
        limit=effective_limit,
        tickers=ticker_scope or None,
    )
    summary = _summary(session, rows, repair_result, settings=resolved, limit=effective_limit)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AR",
        "phase_version": PHASE_3AR_VERSION,
        "mode": "PAPER_ONLY_CRYPTO_FORECAST_COVERAGE_REPAIR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "settings": {
            "crypto_v2_min_link_confidence": str(resolved.crypto_v2_min_link_confidence),
            "crypto_v2_min_history_minutes": resolved.crypto_v2_min_history_minutes,
        },
        "diagnostic_scope": {
            "scope": "EXACT_TICKERS" if ticker_scope else "LATEST_CRYPTO_LINKS",
            "ticker_count": len(ticker_scope),
            "requested_ticker_count": len(tickers or []),
            "requested_limit": limit,
            "effective_limit": effective_limit,
            "effective_limit_reason": (
                "exact_ticker_scope"
                if ticker_scope
                else "bounded_default_to_keep_report_terminal"
            ),
        },
        "summary": summary,
        "repair_result": repair_result,
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "skip_reason_counts": _skip_reason_counts(session),
        "rows": rows,
        "top_blocked": [row for row in rows if row["status"] != STATUS_READY][:50],
        "recommended_next_action": _recommended_next_action(summary),
        "next_commands": [
            "kalshi-bot ingest-crypto --symbols BTC,ETH,SOL,XRP,DOGE --source coinbase",
            "kalshi-bot build-crypto-features --symbols BTC,ETH,SOL,XRP,DOGE",
            "kalshi-bot link-crypto-markets",
            (
                "kalshi-bot phase3ar-crypto-forecast-coverage --repair-snapshots "
                "--output-dir reports/phase3ar --limit 5000"
            ),
            "kalshi-bot forecast --model crypto_v2",
        ],
    }


def repair_crypto_linked_snapshots(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int = 500,
    client: CryptoSnapshotClient | None = None,
    candidate_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Collect fresh snapshots for linked crypto markets that are blocked by snapshot gaps."""
    resolved = settings or get_settings()
    rows = candidate_rows or _diagnostic_rows(session, settings=resolved, limit=limit)
    candidates = [
        row
        for row in rows
        if row["status"]
        in {STATUS_NO_SNAPSHOT, STATUS_FUTURE_FEATURE, STATUS_NO_MIDPOINT, STATUS_STALE_QUOTE}
    ][:limit]
    owned_client: KalshiClient | None = None
    if client is None and candidates:
        owned_client = KalshiClient()
        client = owned_client

    repair_rows: list[dict[str, Any]] = []
    try:
        for row in candidates:
            if client is None:
                break
            repair_rows.append(_repair_one_snapshot(session, row["ticker"], client=client))
    finally:
        if owned_client is not None:
            owned_client.close()

    counts = Counter(item["status"] for item in repair_rows)
    return {
        "attempted": len(repair_rows),
        "repaired": counts.get("repaired", 0),
        "still_missing": len(repair_rows) - counts.get("repaired", 0),
        "status_counts": dict(sorted(counts.items())),
        "rows": repair_rows[:100],
    }


def repair_crypto_snapshots_for_tickers(
    session: Session,
    tickers: list[str],
    *,
    limit: int = 50,
    client: CryptoSnapshotClient | None = None,
) -> dict[str, Any]:
    """Collect fresh exact-ticker snapshots for a bounded set of crypto markets."""
    candidates = _unique_tickers(tickers)[: max(0, limit)]
    owned_client: KalshiClient | None = None
    if client is None and candidates:
        owned_client = KalshiClient()
        client = owned_client

    repair_rows: list[dict[str, Any]] = []
    try:
        for ticker in candidates:
            if client is None:
                break
            repair_rows.append(_repair_one_snapshot(session, ticker, client=client))
    finally:
        if owned_client is not None:
            owned_client.close()

    counts = Counter(item["status"] for item in repair_rows)
    return {
        "mode": "PAPER_ONLY_EXACT_TICKER_SNAPSHOT_REFRESH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "requested": len(tickers),
        "attempted": len(repair_rows),
        "repaired": counts.get("repaired", 0),
        "status_counts": dict(sorted(counts.items())),
        "rows": repair_rows[:100],
    }


def write_phase3ar_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    settings: Settings | None = None,
    limit: int = 500,
    repair_snapshots: bool = False,
    client: CryptoSnapshotClient | None = None,
    tickers: list[str] | None = None,
) -> Phase3ARArtifactSet:
    payload = build_crypto_forecast_coverage(
        session,
        settings=settings,
        limit=limit,
        repair_snapshots=repair_snapshots,
        client=client,
        tickers=tickers,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ar_crypto_forecast_coverage.json"
    markdown_path = output_dir / "phase3ar_crypto_forecast_coverage.md"
    rows_path = output_dir / "crypto_forecast_coverage_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(json.dumps(payload["rows"], indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3ARArtifactSet(output_dir, json_path, markdown_path, rows_path)


def build_phase3ar_url_audit(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    phase3aq = _phase3aq_link_audit(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    rows = [
        _phase3ar_url_audit_row(session, row, settings=resolved)
        for row in phase3aq["positive_ev_rows"]
    ]
    reason_counts = Counter(row["specific_malformed_reason"] for row in rows if row["specific_malformed_reason"])
    previous_reason_counts = Counter(
        row["previous_malformed_reason"] for row in rows if row["previous_malformed_reason"]
    )
    status_counts = Counter(row["current_url_status"] for row in rows)
    proposed_counts = Counter(row["proposed_url_status"] for row in rows)
    metadata = _phase3ar_metadata(
        session,
        settings=resolved,
        output_dir=output_dir,
        command="kalshi-bot phase3ar-url-audit",
        command_args={
            "output_dir": str(output_dir),
            "reports_dir": str(reports_dir),
            "window_hours": window_hours,
            "limit": limit,
        },
    )
    return {
        "generated_at": metadata["generated_at"],
        "phase": "3AR",
        "phase_version": PHASE_3AR_LINK_REPAIR_VERSION,
        "mode": "PAPER_READ_ONLY_KALSHI_URL_AUDIT",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "fake_links_created": False,
        "sibling_or_fuzzy_matching_allowed": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "summary": {
            "positive_ev_rows": len(rows),
            "current_positive_ev_rows": len(rows),
            "all_positive_ev_rows_including_diagnostics": phase3aq["summary"].get(
                "all_positive_ev_rows_including_diagnostics",
                len(rows),
            ),
            "expired_positive_ev_rows": phase3aq["summary"].get("expired_positive_ev_rows", 0),
            "expired_excluded_rows": phase3aq["summary"].get("expired_excluded_rows", 0),
            "historical_diagnostic_rows": phase3aq["summary"].get("historical_diagnostic_rows", 0),
            "finalized_or_settled_rows": phase3aq["summary"].get("finalized_or_settled_rows", 0),
            "stale_catalog_rows": phase3aq["summary"].get("stale_catalog_rows", 0),
            "stale_quote_rows": phase3aq["summary"].get("stale_quote_rows", 0),
            "first_hard_blocker": phase3aq["summary"].get("first_hard_blocker"),
            "exact_catalog_matches": sum(1 for row in rows if row["canonical_catalog_match"]),
            "current_verified_links": sum(1 for row in rows if row["current_url_status"] == VERIFIED),
            "current_malformed_urls": sum(1 for row in rows if row["current_url_status"] == MALFORMED_URL),
            "safe_to_persist": sum(1 for row in rows if row["safe_to_persist"]),
            "manual_review_required": sum(1 for row in rows if row["manual_review_required"]),
            "current_url_status_counts": dict(sorted(status_counts.items())),
            "specific_malformed_reason_counts": dict(sorted(reason_counts.items())),
            "previous_malformed_reason_counts": dict(sorted(previous_reason_counts.items())),
            "proposed_url_status_counts": dict(sorted(proposed_counts.items())),
        },
        "allowed_malformed_reason_codes": list(URL_REPAIR_REASON_CODES),
        "rows": rows,
        "current_positive_ev_rows": rows,
        "expired_positive_ev_rows": phase3aq.get("expired_positive_ev_rows", []),
        "historical_diagnostic_rows": phase3aq.get("historical_diagnostic_rows", []),
        "finalized_or_settled_rows": phase3aq.get("finalized_or_settled_rows", []),
        "safe_repair_rows": [row for row in rows if row["safe_to_persist"]],
        "manual_review_rows": [row for row in rows if row["manual_review_required"]],
        "next_action": _phase3ar_url_next_action(rows, phase3aq["summary"]),
    }


def write_phase3ar_url_audit_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> Phase3ARLinkArtifactSet:
    payload = build_phase3ar_url_audit(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "url_audit.json"
    markdown_path = output_dir / "url_audit.md"
    _phase3ar_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3ar_url_audit_markdown(payload), encoding="utf-8")
    return Phase3ARLinkArtifactSet(output_dir, json_path, markdown_path)


def build_phase3ar_catalog_stale_diagnostic(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    audit = build_phase3ar_url_audit(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    latest_market_last_seen_at = session.scalar(select(func.max(Market.last_seen_at)))
    rows = [
        _phase3ar_catalog_stale_row(
            session,
            row,
            settings=resolved,
            latest_market_last_seen_at=latest_market_last_seen_at,
        )
        for row in audit["rows"]
        if row.get("current_url_status") == STALE_CATALOG
        or row.get("specific_malformed_reason") == STALE_CATALOG
        or row.get("exact_blocker_if_not_safe") == STALE_CATALOG
    ]
    reason_counts = Counter(row["stale_reason"] for row in rows)
    metadata = _phase3ar_metadata(
        session,
        settings=resolved,
        output_dir=output_dir,
        command="kalshi-bot phase3ar-catalog-stale-diagnostic",
        command_args={
            "output_dir": str(output_dir),
            "reports_dir": str(reports_dir),
            "window_hours": window_hours,
            "limit": limit,
        },
    )
    return {
        "generated_at": metadata["generated_at"],
        "phase": "3AR-R2",
        "phase_version": PHASE_3AR_LINK_REPAIR_VERSION,
        "mode": "PAPER_READ_ONLY_CATALOG_STALE_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "catalog_metadata_writes": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "summary": {
            "positive_ev_rows": audit["summary"]["positive_ev_rows"],
            "current_positive_ev_rows": audit["summary"].get(
                "current_positive_ev_rows",
                audit["summary"]["positive_ev_rows"],
            ),
            "expired_positive_ev_rows": audit["summary"].get("expired_positive_ev_rows", 0),
            "expired_excluded_rows": audit["summary"].get("expired_excluded_rows", 0),
            "historical_diagnostic_rows": audit["summary"].get("historical_diagnostic_rows", 0),
            "finalized_or_settled_rows": audit["summary"].get("finalized_or_settled_rows", 0),
            "stale_catalog_rows": len(rows),
            "stale_quote_rows": audit["summary"].get("stale_quote_rows", 0),
            "first_hard_blocker": audit["summary"].get("first_hard_blocker"),
            "refreshable_exact_markets": sum(
                1
                for row in rows
                if row["exact_market_exists_in_active_catalog"]
                and row["stale_reason"] != "MARKET_CLOSED_OR_SETTLED"
            ),
            "reason_counts": dict(sorted(reason_counts.items())),
            "latest_market_last_seen_at": (
                latest_market_last_seen_at.isoformat() if latest_market_last_seen_at else None
            ),
            "freshness_threshold_seconds": resolved.phase_3t_stale_after_seconds,
        },
        "allowed_reason_codes": list(CATALOG_STALE_REASON_CODES),
        "rows": rows,
        "next_action": _phase3ar_catalog_stale_next_action(rows),
    }


def write_phase3ar_catalog_stale_diagnostic_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> Phase3ARLinkArtifactSet:
    payload = build_phase3ar_catalog_stale_diagnostic(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "catalog_stale_diagnostic.json"
    markdown_path = output_dir / "catalog_stale_diagnostic.md"
    _phase3ar_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3ar_catalog_stale_markdown(payload), encoding="utf-8")
    return Phase3ARLinkArtifactSet(output_dir, json_path, markdown_path)


def build_phase3ar_refresh_catalog_for_opportunities(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    reports_dir: Path = Path("reports"),
    dry_run: bool = True,
    apply_readonly_refresh: bool = False,
    max_markets: int = 100,
    max_duration_seconds: int = 120,
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
    client: CryptoSnapshotClient | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    audit = build_phase3ar_url_audit(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    diagnostic = build_phase3ar_catalog_stale_diagnostic(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    diagnostic_by_ticker = {
        str(row.get("market_ticker") or ""): row
        for row in diagnostic.get("rows", [])
        if isinstance(row, dict)
    }
    before_exact_rows = [
        _phase3ar_catalog_handoff_row(session, row, settings=resolved)
        for row in audit.get("rows", [])
        if isinstance(row, dict)
    ]
    exact_tickers = [row["market_ticker"] for row in before_exact_rows if row.get("market_ticker")]
    candidates: list[dict[str, Any]] = []
    for row in before_exact_rows:
        ticker = str(row.get("market_ticker") or "").strip()
        if not ticker or row.get("exact_catalog_fresh"):
            continue
        lifecycle = str(row.get("lifecycle_status") or "").strip()
        if lifecycle and is_inactive_market_status(lifecycle):
            continue
        diagnostic_row = diagnostic_by_ticker.get(ticker, {})
        candidates.append(
            {
                **row,
                "stale_reason": diagnostic_row.get("stale_reason") or row.get("catalog_freshness_reason"),
                "exact_market_exists_in_active_catalog": bool(row.get("exact_market_exists_in_active_catalog")),
                "exact_market_exists": bool(row.get("exact_market_exists")),
                "next_action": "Refresh this exact ticker with the Kalshi /markets/{ticker} endpoint.",
            }
        )
        if len(candidates) >= max(0, max_markets):
            break
    writer = _phase3ar_db_writer_status(settings=resolved)
    refreshed_rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    blocked_reason = None
    status = "DRY_RUN" if candidates else "NO_REFRESH_CANDIDATES"
    started_at = utc_now()
    owned_client: KalshiClient | None = None

    if apply_readonly_refresh and not bool(writer.get("safe_to_start_write", True)):
        status = "BLOCKED_BY_ACTIVE_WRITER"
        blocked_reason = "Another DB writer owns the database."
    elif apply_readonly_refresh and candidates:
        deadline = time.monotonic() + max(1, max_duration_seconds)
        status = "READONLY_REFRESH_COMPLETED"
        try:
            if client is None:
                owned_client = KalshiClient(settings=resolved)
                client = owned_client
            for row in candidates:
                if time.monotonic() >= deadline:
                    status = "READONLY_REFRESH_PARTIAL_DEADLINE_EXCEEDED"
                    break
                ticker = row["market_ticker"]
                try:
                    market_json = dict(client.get_market(ticker))
                except KalshiRetryError as exc:
                    failed_rows.append(
                        {
                            "market_ticker": ticker,
                            "status": RATE_LIMITED_RETRY_EXHAUSTED,
                            "error": str(exc),
                        }
                    )
                    status = RATE_LIMITED_RETRY_EXHAUSTED
                    break
                except Exception as exc:  # noqa: BLE001 - external read endpoint failures are report rows.
                    failed_rows.append(
                        {
                            "market_ticker": ticker,
                            "status": "FETCH_FAILED",
                            "error": str(exc),
                        }
                    )
                    continue
                fetched_ticker = str(market_json.get("ticker") or "").strip()
                if fetched_ticker != ticker:
                    failed_rows.append(
                        {
                            "market_ticker": ticker,
                            "status": "TICKER_MISMATCH",
                            "fetched_ticker": fetched_ticker,
                        }
                    )
                    continue
                before = session.get(Market, ticker)
                before_last_seen = before.last_seen_at.isoformat() if before and before.last_seen_at else None
                refreshed = upsert_market(session, market_json)
                refreshed_raw = decode_json(refreshed.raw_json)
                refreshed_url = build_canonical_kalshi_url(market=refreshed, settings=resolved)
                refreshed_rows.append(
                    {
                        "market_ticker": ticker,
                        "status": "REFRESHED",
                        "previous_last_seen_at": before_last_seen,
                        "catalog_last_seen_at": refreshed.last_seen_at.isoformat()
                        if refreshed.last_seen_at
                        else None,
                        "lifecycle_status": refreshed.status,
                        "title": refreshed.title,
                        "event_ticker": refreshed.event_ticker,
                        "series_ticker": refreshed.series_ticker,
                        "event_slug": _phase3ar_raw_slug(refreshed_raw),
                        "url_verification_status": _phase3ar_public_url_status(
                            refreshed_url.kalshi_url_status
                        ),
                        "kalshi_url": refreshed_url.kalshi_url
                        if refreshed_url.kalshi_url_status == VERIFIED
                        else None,
                    }
                )
            session.flush()
            session.commit()
        finally:
            if owned_client is not None:
                owned_client.close()

    rate_limit = _phase3ar_rate_limit_summary(
        client,
        rows_fetched_before_limit=len(refreshed_rows),
    )
    if apply_readonly_refresh and rate_limit.get("rate_limited"):
        if status == RATE_LIMITED_RETRY_EXHAUSTED:
            pass
        elif refreshed_rows or failed_rows:
            status = str(rate_limit.get("status") or RATE_LIMITED_PARTIAL)
        else:
            status = RATE_LIMITED_ABORTED
            rate_limit["status"] = RATE_LIMITED_ABORTED
    refreshed_tickers = {row["market_ticker"] for row in refreshed_rows}
    failed_by_ticker = {row["market_ticker"]: row for row in failed_rows}
    exact_catalog_handoff_rows = [
        _phase3ar_catalog_handoff_row(
            session,
            row,
            settings=resolved,
            refresh_status=_phase3ar_catalog_refresh_row_status(
                row,
                refreshed_tickers=refreshed_tickers,
                failed_by_ticker=failed_by_ticker,
                rate_limited=bool(rate_limit.get("rate_limited")),
            ),
            refresh_error=failed_by_ticker.get(str(row.get("market_ticker") or ""), {}).get("error"),
        )
        for row in audit.get("rows", [])
        if isinstance(row, dict)
    ]
    handoff_summary = _phase3ar_catalog_handoff_summary(
        exact_catalog_handoff_rows,
        rate_limit=rate_limit,
    )

    metadata = _phase3ar_metadata(
        session,
        settings=resolved,
        output_dir=output_dir,
        command="kalshi-bot phase3ar-refresh-catalog-for-opportunities",
        command_args={
            "output_dir": str(output_dir),
            "reports_dir": str(reports_dir),
            "dry_run": dry_run,
            "apply_readonly_refresh": apply_readonly_refresh,
            "max_markets": max_markets,
            "max_duration_seconds": max_duration_seconds,
            "window_hours": window_hours,
            "limit": limit,
        },
    )
    freshness_views = _phase3ar_catalog_freshness_views(
        metadata=metadata,
        reports_dir=reports_dir,
        audit=audit,
        handoff_summary=handoff_summary,
        handoff_rows=exact_catalog_handoff_rows,
        rate_limit=rate_limit,
    )
    summary = {
        "positive_ev_rows": audit["summary"]["positive_ev_rows"],
        "current_positive_ev_rows": audit["summary"].get(
            "current_positive_ev_rows",
            audit["summary"]["positive_ev_rows"],
        ),
        "expired_positive_ev_rows": audit["summary"].get("expired_positive_ev_rows", 0),
        "expired_excluded_rows": audit["summary"].get("expired_excluded_rows", 0),
        "historical_diagnostic_rows": audit["summary"].get("historical_diagnostic_rows", 0),
        "finalized_or_settled_rows": audit["summary"].get("finalized_or_settled_rows", 0),
        "stale_catalog_rows": diagnostic["summary"]["stale_catalog_rows"],
        "stale_quote_rows": audit["summary"].get("stale_quote_rows", 0),
        "first_hard_blocker": audit["summary"].get("first_hard_blocker"),
        "refresh_candidates": len(candidates),
        "refreshed_rows": len(refreshed_rows),
        "failed_rows": len(failed_rows),
        "blocked_by_active_writer": status == "BLOCKED_BY_ACTIVE_WRITER",
        **handoff_summary,
    }
    return {
        "generated_at": metadata["generated_at"],
        "started_at": started_at.isoformat(),
        "phase": "3AR-R2",
        "phase_version": PHASE_3AR_LINK_REPAIR_VERSION,
        "mode": "PAPER_READ_ONLY_EXACT_CATALOG_REFRESH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "dry_run": dry_run and not apply_readonly_refresh,
        "apply_readonly_refresh": apply_readonly_refresh,
        "catalog_metadata_writes": bool(apply_readonly_refresh and refreshed_rows),
        "feature_writes": False,
        "forecast_writes": False,
        "opportunity_writes": False,
        "settlement_writes": False,
        "status": status,
        "blocked_reason": blocked_reason,
        "active_writer": writer,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "summary": summary,
        "rate_limit": rate_limit,
        "freshness_views": freshness_views,
        "exact_positive_ev_tickers": exact_tickers,
        "before_exact_catalog_rows": before_exact_rows,
        "exact_catalog_handoff_rows": exact_catalog_handoff_rows,
        "refresh_candidates": candidates,
        "refreshed_rows": refreshed_rows,
        "failed_rows": failed_rows,
        "next_action": _phase3ar_catalog_refresh_next_action(
            status,
            len(candidates),
            len(refreshed_rows),
            exact_ticker_not_refreshed_count=summary["exact_ticker_not_refreshed_rows"],
            rate_limited=bool(rate_limit.get("rate_limited")),
        ),
    }


def write_phase3ar_refresh_catalog_for_opportunities_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    reports_dir: Path = Path("reports"),
    dry_run: bool = True,
    apply_readonly_refresh: bool = False,
    max_markets: int = 100,
    max_duration_seconds: int = 120,
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
    client: CryptoSnapshotClient | None = None,
) -> Phase3ARLinkArtifactSet:
    payload = build_phase3ar_refresh_catalog_for_opportunities(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        dry_run=dry_run,
        apply_readonly_refresh=apply_readonly_refresh,
        max_markets=max_markets,
        max_duration_seconds=max_duration_seconds,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
        client=client,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "catalog_refresh_plan.json"
    markdown_path = output_dir / "catalog_refresh_plan.md"
    _phase3ar_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3ar_catalog_refresh_markdown(payload), encoding="utf-8")
    return Phase3ARLinkArtifactSet(output_dir, json_path, markdown_path)


def build_phase3ar_url_repair(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    reports_dir: Path = Path("reports"),
    dry_run: bool = True,
    apply: bool = False,
    backup_first: bool = False,
    max_records: int = 100,
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    audit = build_phase3ar_url_audit(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    candidates = audit["safe_repair_rows"][: max(0, max_records)]
    writer = _phase3ar_db_writer_status(settings=resolved)
    status = "DRY_RUN"
    backup_path = None
    repaired_rows: list[dict[str, Any]] = []
    blocked_reason = None
    run_id = utc_now().strftime("phase3ar-%Y%m%dT%H%M%SZ")
    if apply and not backup_first:
        status = "BLOCKED_REQUIRES_BACKUP_FIRST"
        blocked_reason = "Apply mode requires --backup-first."
    elif apply and not bool(writer.get("safe_to_start_write", True)):
        status = "BLOCKED_BY_ACTIVE_WRITER"
        blocked_reason = "Another DB writer owns the database."
    elif apply:
        output_dir.mkdir(parents=True, exist_ok=True)
        backup_path = str(_phase3ar_write_url_backup(output_dir, candidates, run_id=run_id))
        for row in candidates:
            market = session.get(Market, row["market_ticker"])
            if market is None:
                continue
            raw = decode_json(market.raw_json)
            previous_url = _phase3ar_raw_url(raw)
            raw.update(
                {
                    "kalshi_url": row["proposed_official_url"],
                    "official_kalshi_url": row["proposed_official_url"],
                    "event_slug": row["proposed_event_slug"],
                    "series_slug": row["proposed_series_slug"],
                    "url_builder_version": row["url_builder_version"],
                    "url_verification_status": VERIFIED,
                    "url_verified_at": utc_now().isoformat(),
                    "phase3ar_repair_run_id": run_id,
                    "phase3ar_previous_url": previous_url,
                    "phase3ar_previous_url_status": row["current_url_status"],
                    "phase3ar_previous_malformed_reason": row["specific_malformed_reason"],
                }
            )
            market.raw_json = encode_json(raw)
            repaired_rows.append(
                {
                    "market_ticker": row["market_ticker"],
                    "previous_url": previous_url,
                    "official_kalshi_url": row["proposed_official_url"],
                    "url_builder_version": row["url_builder_version"],
                    "repair_run_id": run_id,
                }
            )
        session.flush()
        session.commit()
        status = "APPLIED" if repaired_rows else "NO_REPAIRABLE_ROWS"
    metadata = _phase3ar_metadata(
        session,
        settings=resolved,
        output_dir=output_dir,
        command="kalshi-bot phase3ar-url-repair",
        command_args={
            "output_dir": str(output_dir),
            "reports_dir": str(reports_dir),
            "dry_run": dry_run,
            "apply": apply,
            "backup_first": backup_first,
            "max_records": max_records,
            "window_hours": window_hours,
            "limit": limit,
        },
    )
    return {
        "generated_at": metadata["generated_at"],
        "phase": "3AR",
        "phase_version": PHASE_3AR_LINK_REPAIR_VERSION,
        "mode": "PAPER_URL_REPAIR_METADATA_ONLY",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "market_data_writes": False,
        "url_catalog_metadata_writes": bool(apply and repaired_rows),
        "feature_writes": False,
        "forecast_writes": False,
        "opportunity_writes": False,
        "settlement_writes": False,
        "dry_run": dry_run,
        "apply": apply,
        "backup_first": backup_first,
        "status": status,
        "blocked_reason": blocked_reason,
        "backup_path": backup_path,
        "active_writer": writer,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "summary": {
            "positive_ev_rows": audit["summary"]["positive_ev_rows"],
            "safe_to_persist": len(candidates),
            "repaired_rows": len(repaired_rows),
            "manual_review_required": audit["summary"]["manual_review_required"],
            "metadata_only_writes": bool(apply and repaired_rows),
        },
        "repair_candidates": candidates,
        "repaired_rows": repaired_rows,
        "next_action": _phase3ar_repair_next_action(status, len(candidates), len(repaired_rows)),
    }


def write_phase3ar_url_repair_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    reports_dir: Path = Path("reports"),
    dry_run: bool = True,
    apply: bool = False,
    backup_first: bool = False,
    max_records: int = 100,
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> Phase3ARLinkArtifactSet:
    payload = build_phase3ar_url_repair(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        dry_run=dry_run,
        apply=apply,
        backup_first=backup_first,
        max_records=max_records,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = "apply" if apply else "dry_run"
    json_path = output_dir / f"url_repair_{suffix}.json"
    markdown_path = output_dir / f"url_repair_{suffix}.md"
    _phase3ar_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3ar_url_repair_markdown(payload), encoding="utf-8")
    return Phase3ARLinkArtifactSet(output_dir, json_path, markdown_path)


def build_phase3ar_refresh_books_for_verified_links(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    dry_run: bool = True,
    apply_readonly_refresh: bool = False,
    max_markets: int = 100,
    max_duration_seconds: int = 120,
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> dict[str, Any]:
    from kalshi_predictor.phase3aq import build_phase3aq_refresh_verified_opportunity_books

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
    verified_markets = [
        row["market_ticker"]
        for row in payload.get("verified_refresh_candidates", [])
        if isinstance(row, dict)
    ]
    payload.update(
        {
            "phase": "3AR",
            "phase_version": PHASE_3AR_LINK_REPAIR_VERSION,
            "mode": "PAPER_READ_ONLY_BOOK_REFRESH_FOR_VERIFIED_LINKS",
            "verified_url_markets": verified_markets,
            "refresh_scope": "EXACT_VERIFIED_MARKET_TICKERS_ONLY",
            "unverified_refresh_allowed": False,
        }
    )
    return payload


def write_phase3ar_refresh_books_for_verified_links_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    dry_run: bool = True,
    apply_readonly_refresh: bool = False,
    max_markets: int = 100,
    max_duration_seconds: int = 120,
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> Phase3ARLinkArtifactSet:
    payload = build_phase3ar_refresh_books_for_verified_links(
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
    json_path = output_dir / "book_refresh_plan.json"
    markdown_path = output_dir / "book_refresh_plan.md"
    _phase3ar_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3ar_book_refresh_markdown(payload), encoding="utf-8")
    return Phase3ARLinkArtifactSet(output_dir, json_path, markdown_path)


def build_phase3ar_settlement_check_noise_audit(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> dict[str, Any]:
    from kalshi_predictor.phase3aq import build_phase3aq_settlement_check_split

    split = build_phase3aq_settlement_check_split(
        session,
        output_dir=output_dir,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    rows = [_phase3ar_settlement_noise_row(row) for row in split["rows"]]
    counts = Counter(row["noise_class"] for row in rows)
    split.update(
        {
            "phase": "3AR",
            "phase_version": PHASE_3AR_LINK_REPAIR_VERSION,
            "mode": "PAPER_READ_ONLY_SETTLEMENT_CHECK_NOISE_AUDIT",
            "rows": rows,
            "noise_class_counts": dict(sorted(counts.items())),
        }
    )
    split["summary"]["noise_class_counts"] = dict(sorted(counts.items()))
    return split


def write_phase3ar_settlement_check_noise_audit_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> Phase3ARLinkArtifactSet:
    payload = build_phase3ar_settlement_check_noise_audit(
        session,
        output_dir=output_dir,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "settlement_check_noise_audit.json"
    markdown_path = output_dir / "settlement_check_noise_audit.md"
    _phase3ar_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3ar_settlement_noise_markdown(payload), encoding="utf-8")
    return Phase3ARLinkArtifactSet(output_dir, json_path, markdown_path)


def write_phase3ar_link_repair_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ar"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = 500,
) -> Phase3ARLinkRepairReportSet:
    resolved = settings or get_settings()
    output_dir.mkdir(parents=True, exist_ok=True)
    audit = build_phase3ar_url_audit(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    catalog_stale = build_phase3ar_catalog_stale_diagnostic(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    catalog_refresh = build_phase3ar_refresh_catalog_for_opportunities(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        dry_run=True,
        apply_readonly_refresh=False,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    dry_run = build_phase3ar_url_repair(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        dry_run=True,
        apply=False,
        backup_first=False,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    book = build_phase3ar_refresh_books_for_verified_links(
        session,
        output_dir=output_dir,
        dry_run=True,
        apply_readonly_refresh=False,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    from kalshi_predictor.phase3aq import build_phase3aq_positive_ev_link_audit

    gate = build_phase3aq_positive_ev_link_audit(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    gate.update(
        {
            "phase": "3AR",
            "phase_version": PHASE_3AR_LINK_REPAIR_VERSION,
            "mode": "PAPER_READY_GATE_AFTER_URL_REPAIR",
        }
    )
    url_audit_path = output_dir / "url_audit.json"
    url_audit_md = output_dir / "url_audit.md"
    catalog_stale_path = output_dir / "catalog_stale_diagnostic.json"
    catalog_stale_md = output_dir / "catalog_stale_diagnostic.md"
    catalog_refresh_path = output_dir / "catalog_refresh_plan.json"
    catalog_refresh_md = output_dir / "catalog_refresh_plan.md"
    dry_run_path = output_dir / "url_repair_dry_run.json"
    book_path = output_dir / "book_refresh_plan.json"
    book_candidates_path = output_dir / "book_refresh_candidates.json"
    gate_path = output_dir / "paper_ready_gate_after_url_repair.json"
    blocked_csv = output_dir / "blocked_positive_ev_rows.csv"
    executive_summary = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions = output_dir / "NEXT_ACTIONS.md"
    manifest = output_dir / "MANIFEST.sha256"
    _phase3ar_write_json(url_audit_path, audit)
    url_audit_md.write_text(_render_phase3ar_url_audit_markdown(audit), encoding="utf-8")
    _phase3ar_write_json(catalog_stale_path, catalog_stale)
    catalog_stale_md.write_text(_render_phase3ar_catalog_stale_markdown(catalog_stale), encoding="utf-8")
    _phase3ar_write_json(catalog_refresh_path, catalog_refresh)
    catalog_refresh_md.write_text(_render_phase3ar_catalog_refresh_markdown(catalog_refresh), encoding="utf-8")
    _phase3ar_write_json(dry_run_path, dry_run)
    _phase3ar_write_json(book_path, book)
    _phase3ar_write_json(book_candidates_path, book)
    _phase3ar_write_json(gate_path, gate)
    _phase3ar_write_csv(blocked_csv, gate.get("blocked_positive_ev_rows", []))
    executive_summary.write_text(
        _render_phase3ar_executive_summary(audit, catalog_stale, catalog_refresh, dry_run, book, gate),
        encoding="utf-8",
    )
    next_actions.write_text(
        _render_phase3ar_next_actions(audit, catalog_stale, catalog_refresh, dry_run, book, gate),
        encoding="utf-8",
    )
    _phase3ar_write_manifest(
        manifest,
        [
            executive_summary,
            next_actions,
            url_audit_path,
            url_audit_md,
            catalog_stale_path,
            catalog_stale_md,
            catalog_refresh_path,
            catalog_refresh_md,
            dry_run_path,
            book_path,
            book_candidates_path,
            gate_path,
            blocked_csv,
        ],
    )
    return Phase3ARLinkRepairReportSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary,
        next_actions_path=next_actions,
        url_audit_path=url_audit_path,
        url_audit_markdown_path=url_audit_md,
        catalog_stale_diagnostic_path=catalog_stale_path,
        catalog_stale_diagnostic_markdown_path=catalog_stale_md,
        catalog_refresh_plan_path=catalog_refresh_path,
        catalog_refresh_plan_markdown_path=catalog_refresh_md,
        url_repair_dry_run_path=dry_run_path,
        book_refresh_plan_path=book_path,
        book_refresh_candidates_path=book_candidates_path,
        paper_ready_gate_path=gate_path,
        blocked_positive_ev_csv_path=blocked_csv,
        manifest_path=manifest,
    )


def _diagnostic_rows(
    session: Session,
    *,
    settings: Settings,
    limit: int,
    tickers: list[str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for link in _latest_links(session, limit=limit, tickers=tickers):
        rows.append(_diagnostic_row(session, link, settings=settings))
    return rows


def _latest_links(
    session: Session,
    *,
    limit: int,
    tickers: list[str] | None = None,
) -> list[CryptoMarketLink]:
    ticker_scope = _unique_tickers(tickers or [])
    if ticker_scope:
        row_number = (
            func.row_number()
            .over(
                partition_by=CryptoMarketLink.ticker,
                order_by=[desc(CryptoMarketLink.detected_at), desc(CryptoMarketLink.id)],
            )
            .label("row_number")
        )
        subquery = (
            select(CryptoMarketLink.id.label("id"), row_number)
            .where(CryptoMarketLink.ticker.in_(ticker_scope))
            .subquery()
        )
        statement = (
            select(CryptoMarketLink)
            .join(subquery, CryptoMarketLink.id == subquery.c.id)
            .where(subquery.c.row_number == 1)
            .order_by(desc(CryptoMarketLink.detected_at), desc(CryptoMarketLink.id))
            .limit(max(0, limit))
        )
        return list(session.scalars(statement))
    return [
        link
        for link in latest_links_for_table(session, CryptoMarketLink, limit=limit)
        if isinstance(link, CryptoMarketLink)
    ]


def _diagnostic_row(
    session: Session,
    link: CryptoMarketLink,
    *,
    settings: Settings,
) -> dict[str, Any]:
    market = session.get(Market, link.ticker)
    snapshot = _latest_snapshot(session, link.ticker)
    terms = terms_from_link_payload(link.symbol, link.raw_json)
    link_confidence = to_decimal(link.confidence) or Decimal("0")
    components = _component_rows(session, terms=terms, snapshot=snapshot)
    forecast = _latest_forecast(session, link.ticker)
    skip = _latest_skip(session, link.ticker)
    market_mid = _market_midpoint(snapshot) if snapshot is not None else None
    window = current_market_window_status(market, settings=settings, now=utc_now())
    status = _coverage_status(
        market=market,
        snapshot=snapshot,
        window=window,
        terms=terms,
        components=components,
        link_confidence=link_confidence,
        settings=settings,
        market_mid=market_mid,
    )
    return {
        "ticker": link.ticker,
        "title": market.title if market is not None else None,
        "market_status": market.status if market is not None else None,
        "active_universe_status": _active_universe_status(market.status if market else None),
        "window_status": window.get("window_status"),
        "current_window_status": window.get("current_window_status"),
        "window_status_reason": window.get("window_status_reason"),
        "current_window_eligible": window.get("current_window_eligible"),
        "current_positive_ev_eligible": window.get("current_positive_ev_eligible"),
        "diagnostic_only": window.get("diagnostic_only"),
        "expired_window_excluded": window.get("expired_window_excluded"),
        "market_close_time": window.get("market_close_time"),
        "expected_expiration_time": window.get("expected_expiration_time"),
        "final_entry_cutoff_time": window.get("final_entry_cutoff_time"),
        "link_deprecated": is_link_deprecated(link),
        "link_id": link.id,
        "link_symbol": link.symbol,
        "link_confidence": link.confidence,
        "link_reason": link.reason,
        "status": status,
        "snapshot_id": snapshot.id if snapshot is not None else None,
        "snapshot_at": snapshot.captured_at.isoformat() if snapshot is not None else None,
        "market_midpoint": str(market_mid) if market_mid is not None else None,
        "terms_status": terms.status if terms is not None else None,
        "component_symbols": list(terms.component_symbols) if terms is not None else [],
        "component_features": components,
        "latest_crypto_v2_forecast_id": forecast.id if forecast is not None else None,
        "latest_crypto_v2_forecast_at": forecast.forecasted_at.isoformat()
        if forecast is not None
        else None,
        "latest_skip_reason": skip.reason if skip is not None else None,
        "latest_skip_at": skip.skipped_at.isoformat() if skip is not None else None,
        "next_action": _next_action_for_status(status, link),
    }


def _component_rows(
    session: Session,
    *,
    terms: CryptoMarketTerms | None,
    snapshot: MarketSnapshot | None,
) -> list[dict[str, Any]]:
    if terms is None:
        return []
    rows: list[dict[str, Any]] = []
    for symbol in terms.component_symbols:
        latest = get_latest_crypto_features(session, symbol)
        if snapshot is None:
            compatibility = None
            ok = False
            reason = "missing_market_snapshot"
            details: dict[str, Any] = {}
            feature = None
        else:
            compatibility = select_compatible_crypto_feature(
                session,
                symbol=symbol,
                terms=terms,
                forecast_cutoff=snapshot.captured_at,
            )
            ok = compatibility.ok
            reason = compatibility.reason
            details = compatibility.details or {}
            feature = compatibility.feature
        rows.append(
            {
                "symbol": symbol,
                "ok": ok,
                "reason": reason,
                "feature_id": feature.id if feature is not None else None,
                "feature_generated_at": feature.generated_at.isoformat()
                if feature is not None
                else None,
                "latest_feature_id": latest.id if latest is not None else None,
                "latest_feature_generated_at": latest.generated_at.isoformat()
                if latest is not None
                else None,
                "history_minutes": _history_minutes(feature),
                "momentum_score": feature.momentum_score if feature is not None else None,
                "details": details,
                "compatibility_checked": compatibility is not None,
            }
        )
    return rows


def _coverage_status(
    *,
    market: Market | None,
    snapshot: MarketSnapshot | None,
    window: dict[str, Any],
    terms: CryptoMarketTerms | None,
    components: list[dict[str, Any]],
    link_confidence: Decimal,
    settings: Settings,
    market_mid: Decimal | None,
) -> str:
    if market is None:
        return STATUS_NO_MARKET
    window_status = str(window.get("window_status") or "")
    if window_status == EXPIRED_WINDOW_EXCLUDED:
        return STATUS_EXPIRED_WINDOW_EXCLUDED
    if window_status == MARKET_CLOSED_OR_SETTLED or is_inactive_market_status(market.status):
        return STATUS_CLOSED_MARKET
    if not window.get("current_window_eligible"):
        return window_status or STATUS_CLOSED_MARKET
    if link_confidence < settings.crypto_v2_min_link_confidence:
        return STATUS_LOW_CONFIDENCE
    if terms is None or terms.status != EXACT_LINK or not terms.component_symbols:
        return STATUS_AMBIGUOUS_TERMS
    if snapshot is None:
        return STATUS_NO_SNAPSHOT
    snapshot_age = _age_minutes(snapshot.captured_at)
    if snapshot_age is None or snapshot_age > QUOTE_STALE_AFTER_MINUTES:
        return STATUS_STALE_QUOTE
    if market_mid is None:
        return STATUS_NO_MIDPOINT
    reasons = {str(row["reason"]) for row in components if not row["ok"]}
    if "future_feature" in reasons or "future_source_timestamp" in reasons:
        return STATUS_FUTURE_FEATURE
    if "stale_feature" in reasons:
        return STATUS_STALE_FEATURE
    if any(not row["ok"] for row in components):
        return STATUS_MISSING_FEATURE
    histories = [_history_int(row.get("history_minutes")) for row in components]
    if not histories or any(
        value is None or value < settings.crypto_v2_min_history_minutes for value in histories
    ):
        return STATUS_INSUFFICIENT_HISTORY
    if any(row.get("momentum_score") is None for row in components):
        return STATUS_NO_MOMENTUM
    return STATUS_READY


def _repair_one_snapshot(
    session: Session,
    ticker: str,
    *,
    client: CryptoSnapshotClient,
) -> dict[str, Any]:
    try:
        market = client.get_market(ticker)
    except KalshiAPIError as exc:
        return {
            "ticker": ticker,
            "status": "api_not_found" if _looks_like_not_found(exc) else "collection_error",
            "error": str(exc),
        }
    except Exception as exc:  # pragma: no cover - protects external adapters.
        return {"ticker": ticker, "status": "collection_error", "error": str(exc)}

    status = str(market.get("status") or "").lower()
    if is_inactive_market_status(status):
        upsert_market(session, market)
        return {"ticker": ticker, "status": "market_closed", "market_status": status}

    orderbook: Mapping[str, Any] | None
    try:
        orderbook = client.get_orderbook(ticker)
    except Exception as exc:  # pragma: no cover - orderbook can legitimately be absent.
        orderbook = None
        error = str(exc)
    else:
        error = None

    snapshot = insert_market_snapshot(session, market, orderbook, captured_at=utc_now())
    status = "repaired" if _market_midpoint(snapshot) is not None else "missing_orderbook"
    row = {
        "ticker": ticker,
        "status": status,
        "snapshot_id": snapshot.id,
        "snapshot_at": snapshot.captured_at.isoformat(),
    }
    if error:
        row["error"] = error
    return row


def _summary(
    session: Session,
    rows: list[dict[str, Any]],
    repair_result: dict[str, Any],
    *,
    settings: Settings,
    limit: int,
) -> dict[str, Any]:
    ready_rows = [row for row in rows if row["status"] == STATUS_READY]
    active_rows = [row for row in rows if row["active_universe_status"] == "active"]
    inactive_rows = [row for row in rows if row["active_universe_status"] == "inactive"]
    unknown_rows = [row for row in rows if row["active_universe_status"] == "unknown"]
    active_blocked_rows = [row for row in active_rows if row["status"] != STATUS_READY]
    linked_with_snapshots = sum(1 for row in rows if row["snapshot_id"] is not None)
    active_linked_with_snapshots = sum(
        1 for row in active_rows if row["snapshot_id"] is not None
    )
    forecasts = int(
        session.scalar(
            select(func.count()).select_from(Forecast).where(Forecast.model_name == "crypto_v2")
        )
        or 0
    )
    blocked_counts = Counter(row["status"] for row in rows if row["status"] != STATUS_READY)
    active_blocked_counts = Counter(row["status"] for row in active_blocked_rows)
    main_active_blocker = (
        active_blocked_counts.most_common(1)[0][0] if active_blocked_counts else None
    )
    gate_summary: dict[str, Any] = {}
    gate_rows: list[dict[str, Any]] = []
    gate_limit = min(max(0, int(limit)), 500)
    try:
        from kalshi_predictor.phase3ap import build_phase3ap_paper_ready_gate

        gate = build_phase3ap_paper_ready_gate(session, settings=settings, limit=gate_limit)
        gate_summary = gate.get("summary", {}) if isinstance(gate.get("summary"), dict) else {}
        gate_rows = gate.get("rows", []) if isinstance(gate.get("rows"), list) else []
    except Exception as exc:  # noqa: BLE001 - coverage report should still render.
        gate_summary = {
            "first_hard_blocker": "PAPER_READY_GATE_SUMMARY_UNAVAILABLE",
            "paper_ready_gate_error": str(exc),
        }
    current_positive_rows = [
        row
        for row in gate_rows
        if (to_decimal(row.get("raw_ev")) or Decimal("0")) > 0
        and bool(row.get("current_positive_ev_eligible"))
    ]
    verified_links = sum(1 for row in current_positive_rows if row.get("kalshi_url_verified"))
    book_refresh_candidates = sum(
        1
        for row in current_positive_rows
        if row.get("kalshi_url_verified") and not row.get("executable_book")
    )
    return {
        "linked_crypto_markets_checked": len(rows),
        "active_linked_crypto_markets": len(active_rows),
        "closed_or_inactive_linked_crypto_markets": len(inactive_rows),
        "unknown_status_linked_crypto_markets": len(unknown_rows),
        "linked_with_snapshots": linked_with_snapshots,
        "active_linked_with_snapshots": active_linked_with_snapshots,
        "ready_to_forecast": len(ready_rows),
        "active_ready_to_forecast": sum(
            1 for row in ready_rows if row["active_universe_status"] == "active"
        ),
        "active_blocked": len(active_blocked_rows),
        "main_active_blocker": main_active_blocker,
        "deprecated_linked_crypto_markets": sum(1 for row in rows if row["link_deprecated"]),
        "blocked": len(rows) - len(ready_rows),
        "main_blocker": main_active_blocker
        or (blocked_counts.most_common(1)[0][0] if blocked_counts else None),
        "current_positive_ev_rows": gate_summary.get("current_positive_ev_rows", 0),
        "positive_ev_rows": gate_summary.get("positive_ev_rows", 0),
        "expired_positive_ev_rows": gate_summary.get("expired_positive_ev_rows", 0),
        "expired_excluded_rows": gate_summary.get("expired_excluded_rows", 0),
        "historical_diagnostic_rows": gate_summary.get("historical_diagnostic_rows", 0),
        "finalized_or_settled_rows": gate_summary.get("finalized_or_settled_rows", 0),
        "stale_catalog_rows": gate_summary.get("stale_catalog_rows", 0),
        "stale_quote_rows": max(
            int(gate_summary.get("stale_quote_rows") or 0),
            sum(1 for row in rows if row["status"] == STATUS_STALE_QUOTE),
        ),
        "verified_links": verified_links,
        "verified_tradeable_links": verified_links,
        "book_refresh_candidates": book_refresh_candidates,
        "book_refresh_needed_rows": book_refresh_candidates,
        "paper_ready_rows": gate_summary.get("paper_ready_rows", 0),
        "first_hard_blocker": gate_summary.get("first_hard_blocker")
        or ("NO_CURRENT_POSITIVE_EV" if not current_positive_rows else main_active_blocker),
        "crypto_v2_forecasts": forecasts,
        "snapshots_repaired": repair_result["repaired"],
        "opportunity_gate_limit": gate_limit,
        "opportunity_gate_limited": gate_limit < max(0, int(limit)),
    }


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _unique_tickers(tickers: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for ticker in tickers:
        value = str(ticker or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _latest_forecast(session: Session, ticker: str) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == "crypto_v2")
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _latest_skip(session: Session, ticker: str) -> ForecastSkipLog | None:
    return session.scalar(
        select(ForecastSkipLog)
        .where(ForecastSkipLog.ticker == ticker, ForecastSkipLog.model_name == "crypto_v2")
        .order_by(desc(ForecastSkipLog.skipped_at), desc(ForecastSkipLog.id))
        .limit(1)
    )


def _market_midpoint(snapshot: MarketSnapshot | None) -> Decimal | None:
    if snapshot is None:
        return None
    bid = to_decimal(snapshot.best_yes_bid)
    ask = to_decimal(snapshot.best_yes_ask)
    if bid is not None and ask is not None:
        return midpoint(bid, ask)
    return to_decimal(snapshot.last_price_dollars)


def _age_minutes(value: Any) -> Decimal | None:
    dt = value if hasattr(value, "astimezone") else parse_datetime(value)
    if dt is None:
        return None
    now = utc_now()
    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return Decimal(str(max(0, (now - dt.astimezone(now.tzinfo)).total_seconds()))) / Decimal("60")


def _history_minutes(feature: CryptoFeature | None) -> int | None:
    if feature is None:
        return None
    raw = decode_json(feature.raw_json)
    value = raw.get("history_minutes")
    return _history_int(value)


def _history_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _skip_reason_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(ForecastSkipLog.reason, func.count(ForecastSkipLog.id))
        .where(ForecastSkipLog.model_name == "crypto_v2")
        .group_by(ForecastSkipLog.reason)
        .order_by(desc(func.count(ForecastSkipLog.id)))
    ).all()
    return {str(reason): int(count) for reason, count in rows}


def _empty_repair_result() -> dict[str, Any]:
    return {"attempted": 0, "repaired": 0, "still_missing": 0, "status_counts": {}, "rows": []}


def _looks_like_not_found(exc: Exception) -> bool:
    text = str(exc).lower()
    return "404" in text or "not found" in text


def _is_closed_market(status: Any) -> bool:
    return is_inactive_market_status(status)


def _active_universe_status(status: Any) -> str:
    if is_inactive_market_status(status):
        return "inactive"
    if is_active_market_status(status):
        return "active"
    return "unknown"


def _next_action_for_status(status: str, link: CryptoMarketLink) -> str:
    if status == STATUS_EXPIRED_WINDOW_EXCLUDED:
        return "Expired crypto window; exclude from current forecasts and paper-ready gates."
    if status == STATUS_CLOSED_MARKET:
        return "Closed market; keep out of new forecasts and sync settlement outcomes."
    if status in {STATUS_NO_SNAPSHOT, STATUS_FUTURE_FEATURE, STATUS_NO_MIDPOINT, STATUS_STALE_QUOTE}:
        return "Run kalshi-bot crypto-forecast-doctor --repair-snapshots, then forecast crypto_v2."
    if status in {STATUS_MISSING_FEATURE, STATUS_STALE_FEATURE, STATUS_INSUFFICIENT_HISTORY}:
        return f"Run ingest/build crypto features for {link.symbol}, then rerun crypto_v2."
    if status == STATUS_LOW_CONFIDENCE:
        return "Run link-crypto-markets and keep low-confidence links out of forecasting."
    if status == STATUS_READY:
        return "Run kalshi-bot forecast --model crypto_v2."
    return "Inspect crypto link semantics before forecasting."


def _recommended_next_action(summary: dict[str, Any]) -> str:
    main = summary.get("main_blocker")
    if summary.get("current_positive_ev_rows") == 0 and summary.get("expired_positive_ev_rows"):
        return "No current positive-EV rows; expired crypto windows are diagnostic-only."
    if main == STATUS_EXPIRED_WINDOW_EXCLUDED:
        return "Collect fresh open crypto windows; expired windows stay excluded from current forecasts."
    if main == STATUS_CLOSED_MARKET:
        return (
            "Collect fresh open crypto markets; closed linked markets should stay out of "
            "forecasting."
        )
    if main in {STATUS_NO_SNAPSHOT, STATUS_FUTURE_FEATURE, STATUS_NO_MIDPOINT, STATUS_STALE_QUOTE}:
        return "Repair snapshots for linked crypto tickers, then rerun crypto_v2."
    if main in {STATUS_MISSING_FEATURE, STATUS_STALE_FEATURE, STATUS_INSUFFICIENT_HISTORY}:
        return "Refresh canonical crypto features for linked symbols, then rerun crypto_v2."
    if summary["ready_to_forecast"] > 0:
        return "Run crypto_v2 forecasts for ready linked markets."
    return "Review blocked rows; ambiguous or low-confidence links should stay excluded."


def _phase3aq_link_audit(
    session: Session,
    *,
    output_dir: Path,
    reports_dir: Path,
    settings: Settings,
    window_hours: int,
    limit: int,
) -> dict[str, Any]:
    from kalshi_predictor.phase3aq import build_phase3aq_positive_ev_link_audit

    return build_phase3aq_positive_ev_link_audit(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )


def _phase3ar_url_audit_row(
    session: Session,
    row: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, Any]:
    ticker = str(row.get("market_ticker") or row.get("ticker") or "").strip()
    market = session.get(Market, ticker) if ticker else None
    raw = decode_json(market.raw_json if market else None)
    current = build_canonical_kalshi_url(market=market, settings=settings)
    proposed = build_canonical_kalshi_url(
        market=market,
        settings=settings,
        allow_deterministic_slug=True,
        allow_stale_proposal=True,
    )
    current_status = _phase3ar_public_url_status(current.kalshi_url_status)
    proposed_status = _phase3ar_public_url_status(proposed.kalshi_url_status)
    stored_url = _phase3ar_raw_url(raw)
    stored_slug = _phase3ar_raw_slug(raw)
    malformed_reason = _phase3ar_malformed_reason(
        ticker=ticker,
        current_status=current_status,
        stored_url=stored_url,
        raw=raw,
    )
    previous_reason = str(raw.get("phase3ar_previous_malformed_reason") or "") or None
    safe_to_persist = (
        current.kalshi_url_status != VERIFIED
        and proposed.kalshi_url_status == BUILT_FROM_EXACT_CATALOG
        and bool(proposed.kalshi_url)
        and ticker
        and market is not None
    )
    blocker = None if safe_to_persist else _phase3ar_url_blocker(current, proposed, malformed_reason)
    return {
        "opportunity_id": row.get("ranking_id") or f"ticker:{ticker}",
        "ranking_id": row.get("ranking_id"),
        "forecast_id": row.get("forecast_id"),
        "market_ticker": ticker,
        "event_ticker": getattr(market, "event_ticker", None) if market else row.get("event_ticker"),
        "series_ticker": getattr(market, "series_ticker", None) if market else row.get("series_ticker"),
        "current_stored_kalshi_url": stored_url,
        "current_stored_slug": stored_slug,
        "current_kalshi_url": stored_url,
        "current_slug": stored_slug,
        "current_url_status": current_status,
        "url_status": current_status,
        "specific_malformed_reason": malformed_reason,
        "previous_malformed_reason": previous_reason,
        "why_url_is_malformed": _phase3ar_reason_text(malformed_reason),
        "url_reason": _phase3ar_reason_text(malformed_reason) or current.kalshi_url_reason,
        "canonical_catalog_match": market is not None,
        "catalog_market_title": getattr(market, "title", None) if market else row.get("market_title"),
        "catalog_title": getattr(market, "title", None) if market else row.get("market_title"),
        "catalog_event_title": raw.get("event_title") or raw.get("event_subtitle") or None,
        "catalog_series_title": raw.get("series_title") or raw.get("series_name") or None,
        "market_lifecycle": getattr(market, "status", None) if market else None,
        "catalog_lifecycle": getattr(market, "status", None) if market else None,
        "catalog_last_seen_at": (
            market.last_seen_at.isoformat() if market is not None and market.last_seen_at else None
        ),
        "window_status": row.get("window_status"),
        "current_window_status": row.get("current_window_status"),
        "window_status_reason": row.get("window_status_reason"),
        "current_positive_ev_eligible": row.get("current_positive_ev_eligible"),
        "diagnostic_only": row.get("diagnostic_only"),
        "market_close_time": row.get("market_close_time"),
        "expected_expiration_time": row.get("expected_expiration_time"),
        "final_entry_cutoff_time": row.get("final_entry_cutoff_time"),
        "proposed_official_url": proposed.kalshi_url,
        "proposed_url": proposed.kalshi_url,
        "proposed_event_slug": proposed.event_slug,
        "proposed_series_slug": proposed.series_slug,
        "proposed_url_status": proposed_status,
        "proposed_url_reason": proposed.kalshi_url_reason,
        "url_builder_version": proposed.builder_version,
        "safe_to_persist": safe_to_persist,
        "manual_review_required": not safe_to_persist and current.kalshi_url_status != VERIFIED,
        "exact_blocker_if_not_safe": blocker,
        "repair_command_required": (
            "kalshi-bot phase3ar-url-repair --apply --backup-first --max-records 100"
            if safe_to_persist
            else None
        ),
        "current_raw_ev": row.get("raw_ev"),
        "book_status": row.get("book_status"),
        "primary_blocker": row.get("primary_blocker"),
        "kalshi_url": current.kalshi_url if current.kalshi_url_status == VERIFIED else None,
        "kalshi_url_verified": current.kalshi_url_status == VERIFIED,
        "next_action": _phase3ar_url_row_next_action(safe_to_persist, current_status, blocker),
    }


def _phase3ar_raw_url(raw: dict[str, Any]) -> str | None:
    for key in ("kalshi_url", "official_kalshi_url", "market_url", "trade_url", "web_url", "event_url", "url"):
        if key in raw:
            value = str(raw.get(key) or "").strip()
            return value or None
    return None


def _phase3ar_has_empty_url_field(raw: dict[str, Any]) -> bool:
    for key in ("kalshi_url", "official_kalshi_url", "market_url", "trade_url", "web_url", "event_url", "url"):
        if key in raw and str(raw.get(key) or "").strip() == "":
            return True
    return False


def _phase3ar_raw_slug(raw: dict[str, Any]) -> str | None:
    for key in ("event_slug", "market_slug", "slug", "event_path", "market_path"):
        value = str(raw.get(key) or "").strip().strip("/")
        if value:
            return value
    return None


def _phase3ar_public_url_status(status: str | None) -> str:
    if status in {CATALOG_STALE, STALE_CATALOG}:
        return STALE_CATALOG
    return str(status or "UNKNOWN_REQUIRES_INVESTIGATION")


def _phase3ar_malformed_reason(
    *,
    ticker: str,
    current_status: str,
    stored_url: str | None,
    raw: dict[str, Any],
) -> str | None:
    ticker_upper = ticker.upper()
    if current_status == VERIFIED:
        return None
    if not ticker:
        return "URL_MISSING_MARKET_TICKER"
    if ticker_upper.startswith("KXMVECROSSCATEGORY-"):
        return "URL_HAS_COMPOSITE_ID"
    if ticker_upper.startswith("KXMVESPORTSMULTIGAMEEXTENDED-"):
        return "URL_HAS_INTERNAL_ID"
    if raw.get("synthetic_only") or raw.get("synthetic_market"):
        return "URL_HAS_SYNTHETIC_ID"
    if current_status == CATALOG_MATCH_MISSING:
        return "CATALOG_MATCH_MISSING"
    if current_status == CATALOG_MATCH_AMBIGUOUS:
        return "CATALOG_MATCH_AMBIGUOUS"
    if current_status in {CATALOG_STALE, STALE_CATALOG}:
        return STALE_CATALOG
    if _phase3ar_has_empty_url_field(raw):
        return "URL_EMPTY"
    if not stored_url:
        return "URL_MISSING"
    parsed = _safe_urlparse(stored_url)
    if parsed is None:
        return "URL_PARSE_FAILED"
    if parsed.scheme not in {"http", "https"}:
        return "URL_NOT_HTTP"
    if parsed.netloc.lower() not in {"kalshi.com", "www.kalshi.com"}:
        return "URL_BAD_DOMAIN"
    if current_status == TICKER_MISMATCH:
        return "URL_TICKER_MISMATCH"
    if not _phase3ar_raw_slug(raw):
        return "URL_SLUG_MISSING"
    return "UNKNOWN_REQUIRES_INVESTIGATION"


def _safe_urlparse(value: str):
    from urllib.parse import urlparse

    try:
        return urlparse(value)
    except Exception:  # noqa: BLE001 - diagnostics should classify malformed input.
        return None


def _phase3ar_url_blocker(
    current: Any,
    proposed: Any,
    malformed_reason: str | None,
) -> str:
    if current.kalshi_url_status == VERIFIED:
        return "ALREADY_VERIFIED"
    if proposed.kalshi_url_status in {CATALOG_STALE, STALE_CATALOG}:
        return STALE_CATALOG
    if proposed.kalshi_url_status in {SYNTHETIC_ONLY, COMPOSITE_LOCAL_ONLY}:
        return proposed.kalshi_url_status
    if proposed.kalshi_url_status in {PLACEHOLDER_BLOCKED, PARTIAL_PROVENANCE_BLOCKED, GENERAL_SOURCE_NOT_SAFE}:
        return proposed.kalshi_url_status
    if proposed.kalshi_url_status == MISSING_MARKET_TICKER:
        return "URL_MISSING_MARKET_TICKER"
    if proposed.kalshi_url_status == CATALOG_MATCH_MISSING:
        return "CATALOG_MATCH_MISSING"
    return malformed_reason or proposed.kalshi_url_status or "UNKNOWN_REQUIRES_INVESTIGATION"


def _phase3ar_reason_text(reason: str | None) -> str | None:
    if reason is None:
        return None
    text = {
        "URL_MISSING": "No trusted Kalshi URL is stored on the exact catalog market row.",
        "URL_EMPTY": "A URL field exists but is empty.",
        "URL_NOT_HTTP": "Stored URL is not an HTTP or HTTPS URL.",
        "URL_BAD_DOMAIN": "Stored URL is not on kalshi.com.",
        "URL_MISSING_MARKET_TICKER": "The opportunity row lacks an exact market ticker.",
        "URL_TICKER_MISMATCH": "Stored URL does not match the exact market or event ticker.",
        "URL_SLUG_MISSING": "Stored catalog row has no trusted slug fields.",
        "CATALOG_MATCH_MISSING": "No exact local catalog market exists.",
        "CATALOG_MATCH_AMBIGUOUS": "Exact catalog identity is ambiguous.",
        "CATALOG_STALE": "Catalog row is stale beyond the configured freshness threshold.",
        "STALE_CATALOG": "Catalog row is stale beyond the configured freshness threshold.",
    }
    return text.get(reason, reason.replace("_", " ").title())


def _phase3ar_url_row_next_action(
    safe_to_persist: bool,
    current_status: str,
    blocker: str | None,
) -> str:
    if current_status == VERIFIED:
        return "URL verified; advance to exact book refresh and paper-ready gates."
    if safe_to_persist:
        return "Run the guarded Phase 3AR URL repair apply command with --backup-first."
    if blocker in {CATALOG_STALE, STALE_CATALOG}:
        return "Run phase3ar-refresh-catalog-for-opportunities, then rerun phase3ar-url-audit."
    return "Keep diagnostic-only and repair exact catalog identity before link persistence."


def _phase3ar_url_next_action(
    rows: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
) -> str:
    summary = summary or {}
    if not rows and int(summary.get("expired_positive_ev_rows") or 0) > 0:
        return "No current positive-EV rows; expired windows are diagnostic-only."
    if not rows and int(summary.get("positive_ev_rows") or 0) == 0:
        return "No current positive-EV rows; keep the watcher running for fresh current windows."
    if any(row["safe_to_persist"] for row in rows):
        return "Run kalshi-bot phase3ar-url-repair --apply --backup-first --max-records 100."
    if any(row["current_url_status"] == VERIFIED for row in rows):
        return "Run kalshi-bot phase3ar-refresh-books-for-verified-links after db-writer-monitor is clear."
    if any(row.get("current_url_status") == STALE_CATALOG for row in rows):
        return (
            "Run kalshi-bot phase3ar-refresh-catalog-for-opportunities --dry-run "
            "--output-dir reports/phase3ar --reports-dir reports."
        )
    return "Repair catalog identity before URL persistence."


def _phase3ar_repair_next_action(status: str, candidate_count: int, repaired_count: int) -> str:
    if status == "DRY_RUN" and candidate_count:
        return "Review url_repair_dry_run.json, then run with --apply --backup-first."
    if status == "APPLIED" and repaired_count:
        return "Run phase3ar-refresh-books-for-verified-links --dry-run."
    if status.startswith("BLOCKED"):
        return "Resolve the blocker and rerun the dry-run before apply."
    return "No URL repair rows remain; run the Phase 3AR link repair report."


def _phase3ar_catalog_stale_row(
    session: Session,
    row: dict[str, Any],
    *,
    settings: Settings,
    latest_market_last_seen_at: Any,
) -> dict[str, Any]:
    ticker = str(row.get("market_ticker") or row.get("ticker") or "").strip()
    market = session.get(Market, ticker) if ticker else None
    raw = decode_json(market.raw_json if market else None)
    catalog_last_seen_at = market.last_seen_at if market is not None else None
    stale_age_seconds = _phase3ar_age_seconds(catalog_last_seen_at)
    threshold = int(settings.phase_3t_stale_after_seconds)
    event_metadata_exists = bool(
        market is not None
        and (
            market.event_ticker
            or raw.get("event_ticker")
            or raw.get("event_title")
            or raw.get("event_subtitle")
            or raw.get("event_name")
        )
    )
    series_metadata_exists = bool(
        market is not None
        and (
            market.series_ticker
            or raw.get("series_ticker")
            or raw.get("series_title")
            or raw.get("series_name")
        )
    )
    slug_or_title_fields_exist = bool(
        market is not None
        and (
            market.title
            or raw.get("title")
            or _phase3ar_raw_slug(raw)
            or raw.get("series_slug")
        )
    )
    lifecycle = str(getattr(market, "status", "") or "").strip()
    exact_active = bool(market is not None and is_active_market_status(lifecycle))
    reason = _phase3ar_catalog_stale_reason(
        market=market,
        lifecycle=lifecycle,
        stale_age_seconds=stale_age_seconds,
        threshold=threshold,
        event_metadata_exists=event_metadata_exists,
        series_metadata_exists=series_metadata_exists,
        slug_or_title_fields_exist=slug_or_title_fields_exist,
    )
    return {
        "market_ticker": ticker,
        "catalog_last_seen_at": catalog_last_seen_at.isoformat() if catalog_last_seen_at else None,
        "latest_market_last_seen_at": (
            latest_market_last_seen_at.isoformat() if latest_market_last_seen_at else None
        ),
        "stale_age_seconds": stale_age_seconds,
        "freshness_threshold_seconds": threshold,
        "lifecycle_status": lifecycle or None,
        "exact_market_exists_in_active_catalog": exact_active,
        "exact_market_exists": market is not None,
        "event_metadata_exists": event_metadata_exists,
        "series_metadata_exists": series_metadata_exists,
        "slug_or_title_fields_exist": slug_or_title_fields_exist,
        "stale_reason": reason,
        "catalog_title": getattr(market, "title", None) if market else row.get("catalog_title"),
        "event_ticker": getattr(market, "event_ticker", None) if market else row.get("event_ticker"),
        "series_ticker": getattr(market, "series_ticker", None) if market else row.get("series_ticker"),
        "proposed_url": row.get("proposed_url") or row.get("proposed_official_url"),
        "next_action": _phase3ar_catalog_stale_row_next_action(reason),
    }


def _phase3ar_catalog_stale_reason(
    *,
    market: Market | None,
    lifecycle: str,
    stale_age_seconds: int | None,
    threshold: int,
    event_metadata_exists: bool,
    series_metadata_exists: bool,
    slug_or_title_fields_exist: bool,
) -> str:
    if market is None:
        return "MARKET_MISSING_FROM_ACTIVE_REFRESH"
    if lifecycle and is_inactive_market_status(lifecycle):
        return "MARKET_CLOSED_OR_SETTLED"
    if not lifecycle:
        return "LIFECYCLE_UNKNOWN"
    if stale_age_seconds is None:
        return "ACTIVE_MARKET_REFRESH_NOT_RUN"
    if stale_age_seconds > threshold:
        return "CATALOG_LAST_SEEN_TOO_OLD"
    if not event_metadata_exists:
        return "EVENT_METADATA_STALE"
    if not series_metadata_exists:
        return "SERIES_METADATA_STALE"
    if not slug_or_title_fields_exist:
        return "SLUG_OR_TITLE_MISSING"
    return "UNKNOWN_REQUIRES_INVESTIGATION"


def _phase3ar_age_seconds(value: Any) -> int | None:
    if value is None:
        return None
    try:
        current = value
        if current.tzinfo is None:
            current = current.replace(tzinfo=utc_now().tzinfo)
        return max(0, int((utc_now() - current).total_seconds()))
    except Exception:
        return None


def _phase3ar_catalog_stale_row_next_action(reason: str) -> str:
    if reason in {"CATALOG_LAST_SEEN_TOO_OLD", "ACTIVE_MARKET_REFRESH_NOT_RUN"}:
        return "Refresh the exact market catalog row with phase3ar-refresh-catalog-for-opportunities."
    if reason == "MARKET_CLOSED_OR_SETTLED":
        return "Keep out of paper entry; closed or settled markets cannot be made paper-ready."
    if reason in {"EVENT_METADATA_STALE", "SERIES_METADATA_STALE", "SLUG_OR_TITLE_MISSING"}:
        return "Refresh exact catalog metadata, then rerun URL audit."
    return "Investigate exact catalog identity before URL persistence."


def _phase3ar_catalog_stale_next_action(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No stale catalog rows; rerun phase3ar-url-audit."
    if any(row["exact_market_exists_in_active_catalog"] for row in rows):
        return (
            "Run kalshi-bot phase3ar-refresh-catalog-for-opportunities --dry-run "
            "--output-dir reports/phase3ar --reports-dir reports."
        )
    return "No active exact catalog rows are refreshable; inspect catalog lineage before URL repair."


def _phase3ar_catalog_handoff_row(
    session: Session,
    row: dict[str, Any],
    *,
    settings: Settings,
    refresh_status: str | None = None,
    refresh_error: str | None = None,
) -> dict[str, Any]:
    ticker = str(row.get("market_ticker") or row.get("ticker") or "").strip()
    market = session.get(Market, ticker) if ticker else None
    raw = decode_json(market.raw_json if market else None)
    snapshot = _latest_snapshot(session, ticker) if ticker else None
    catalog_last_seen_at = market.last_seen_at if market is not None else None
    catalog_age = _phase3ar_age_seconds(catalog_last_seen_at)
    book_age = _phase3ar_age_seconds(snapshot.captured_at) if snapshot is not None else None
    threshold = int(settings.phase_3t_stale_after_seconds)
    lifecycle = str(getattr(market, "status", "") or "").strip()
    event_ticker = getattr(market, "event_ticker", None) if market else row.get("event_ticker")
    series_ticker = getattr(market, "series_ticker", None) if market else row.get("series_ticker")
    event_slug = str(raw.get("event_slug") or raw.get("event_path") or "").strip().strip("/") or None
    series_slug = str(raw.get("series_slug") or raw.get("series_path") or "").strip().strip("/") or None
    market_slug = str(raw.get("market_slug") or raw.get("slug") or "").strip().strip("/") or None
    title = getattr(market, "title", None) if market else row.get("market_title")
    exact_catalog_fresh = bool(
        market is not None
        and catalog_age is not None
        and catalog_age <= threshold
        and lifecycle
        and not is_inactive_market_status(lifecycle)
        and title
        and event_ticker
        and series_ticker
    )
    current = build_canonical_kalshi_url(market=market, settings=settings)
    proposed = build_canonical_kalshi_url(
        market=market,
        settings=settings,
        allow_deterministic_slug=True,
        allow_stale_proposal=True,
    )
    book_midpoint = _market_midpoint(snapshot)
    book_fresh = bool(snapshot is not None and book_age is not None and book_age <= threshold)
    if snapshot is None:
        book_status = "BOOK_MISSING"
    elif not book_fresh:
        book_status = "BOOK_STALE"
    elif book_midpoint is None:
        book_status = "BOOK_NO_EXECUTABLE_MIDPOINT"
    else:
        book_status = "BOOK_FRESH"
    catalog_reason = "COMPLETE" if exact_catalog_fresh else "EXACT_TICKER_NOT_REFRESHED"
    if market is None:
        catalog_reason = "EXACT_MARKET_MISSING_FROM_CATALOG"
    elif lifecycle and is_inactive_market_status(lifecycle):
        catalog_reason = "MARKET_CLOSED_OR_SETTLED"
    elif catalog_age is None:
        catalog_reason = "ACTIVE_MARKET_REFRESH_NOT_RUN"
    elif catalog_age > threshold:
        catalog_reason = "CATALOG_LAST_SEEN_TOO_OLD"
    elif not lifecycle:
        catalog_reason = "LIFECYCLE_UNKNOWN"
    elif not (event_ticker and series_ticker and title):
        catalog_reason = "CATALOG_IDENTITY_METADATA_INCOMPLETE"
    return {
        "market_ticker": ticker,
        "refresh_status": refresh_status or (
            "EXACT_TICKER_ALREADY_FRESH" if exact_catalog_fresh else "EXACT_TICKER_NOT_REFRESHED"
        ),
        "refresh_error": refresh_error,
        "exact_market_exists": market is not None,
        "exact_market_exists_in_active_catalog": bool(
            market is not None and lifecycle and is_active_market_status(lifecycle)
        ),
        "exact_catalog_fresh": exact_catalog_fresh,
        "catalog_freshness_reason": catalog_reason,
        "catalog_last_seen_at": catalog_last_seen_at.isoformat() if catalog_last_seen_at else None,
        "catalog_last_seen_age_seconds": catalog_age,
        "freshness_threshold_seconds": threshold,
        "lifecycle_status": lifecycle or None,
        "title": title,
        "catalog_title": title,
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "event_title": raw.get("event_title") or raw.get("event_subtitle") or None,
        "series_title": raw.get("series_title") or raw.get("series_name") or None,
        "event_slug": event_slug,
        "series_slug": series_slug,
        "market_slug": market_slug,
        "stored_kalshi_url": _phase3ar_raw_url(raw),
        "url_verification_status": _phase3ar_public_url_status(current.kalshi_url_status),
        "url_verification_reason": current.kalshi_url_reason,
        "kalshi_url": current.kalshi_url if current.kalshi_url_status == VERIFIED else None,
        "proposed_url_status": _phase3ar_public_url_status(proposed.kalshi_url_status),
        "proposed_kalshi_url": proposed.kalshi_url,
        "book_snapshot_at": snapshot.captured_at.isoformat() if snapshot is not None else None,
        "book_snapshot_age_seconds": book_age,
        "book_orderbook_status": book_status,
        "book_orderbook_fresh": book_fresh,
        "book_has_executable_midpoint": book_midpoint is not None,
        "data_complete": bool(exact_catalog_fresh and book_fresh),
        "partial_reason": None if exact_catalog_fresh else catalog_reason,
    }


def _phase3ar_catalog_refresh_row_status(
    row: dict[str, Any],
    *,
    refreshed_tickers: set[str],
    failed_by_ticker: dict[str, dict[str, Any]],
    rate_limited: bool,
) -> str:
    ticker = str(row.get("market_ticker") or "").strip()
    if ticker in failed_by_ticker:
        return str(failed_by_ticker[ticker].get("status") or "FETCH_FAILED")
    if ticker in refreshed_tickers:
        return "REFRESHED"
    if bool(row.get("exact_catalog_fresh")):
        return "EXACT_TICKER_ALREADY_FRESH"
    if rate_limited:
        return RATE_LIMITED_PARTIAL
    return "EXACT_TICKER_NOT_REFRESHED"


def _phase3ar_rate_limit_summary(
    client: CryptoSnapshotClient | None,
    *,
    rows_fetched_before_limit: int,
) -> dict[str, Any]:
    telemetry = getattr(client, "telemetry", None) if client is not None else None
    if telemetry is not None and hasattr(telemetry, "as_dict"):
        payload = dict(telemetry.as_dict(rows_fetched_before_limit=rows_fetched_before_limit))
    else:
        payload = {
            "status": "COMPLETE",
            "rate_limited": False,
            "request_count": 0,
            "retry_count": 0,
            "rate_limited_count": 0,
            "retry_exhausted_count": 0,
            "total_sleep_seconds": 0.0,
            "rows_fetched_before_limit": rows_fetched_before_limit,
            "data_completeness": "complete",
            "endpoints": [],
            "events": [],
        }
    rate_limited = bool(payload.get("rate_limited")) or str(payload.get("status") or "") in PHASE3AR_RATE_LIMIT_STATUSES
    payload["rate_limited"] = rate_limited
    payload["data_complete"] = not rate_limited
    payload["data_completeness"] = "partial" if rate_limited else "complete"
    payload["blocker"] = "RATE_LIMITED_KALSHI_API" if rate_limited else None
    payload["affected_stages"] = ["phase3ar_exact_catalog_refresh"] if rate_limited else []
    return payload


def _phase3ar_catalog_handoff_summary(
    rows: list[dict[str, Any]],
    *,
    rate_limit: dict[str, Any],
) -> dict[str, Any]:
    rate_limited = bool(rate_limit.get("rate_limited"))
    exact_not_refreshed = sum(
        1
        for row in rows
        if not bool(row.get("exact_catalog_fresh"))
        and row.get("refresh_status") not in {"REFRESHED", "EXACT_TICKER_ALREADY_FRESH"}
    )
    return {
        "exact_positive_ev_tickers": len({row.get("market_ticker") for row in rows if row.get("market_ticker")}),
        "exact_catalog_rows_checked": len(rows),
        "exact_catalog_fresh_rows": sum(1 for row in rows if row.get("exact_catalog_fresh")),
        "exact_catalog_stale_rows": sum(
            1
            for row in rows
            if row.get("exact_market_exists") and not row.get("exact_catalog_fresh")
        ),
        "exact_catalog_missing_rows": sum(1 for row in rows if not row.get("exact_market_exists")),
        "url_verified_rows": sum(1 for row in rows if row.get("url_verification_status") == VERIFIED),
        "book_fresh_rows": sum(1 for row in rows if row.get("book_orderbook_fresh")),
        "book_executable_rows": sum(1 for row in rows if row.get("book_has_executable_midpoint")),
        "exact_ticker_not_refreshed_rows": exact_not_refreshed,
        "rate_limited_rows": exact_not_refreshed if rate_limited else 0,
        "rate_limit_status": str(rate_limit.get("status") or "COMPLETE"),
        "data_complete": bool(rows) and exact_not_refreshed == 0 and not rate_limited,
        "data_completeness": "partial" if exact_not_refreshed or rate_limited else "complete",
    }


def _phase3ar_catalog_freshness_views(
    *,
    metadata: dict[str, Any] | None,
    reports_dir: Path,
    audit: dict[str, Any],
    handoff_summary: dict[str, Any],
    handoff_rows: list[dict[str, Any]],
    rate_limit: dict[str, Any],
) -> dict[str, Any]:
    top_strip = _phase3ar_read_json(reports_dir / "phase_3ak" / "top_strip_status.json")
    watermark = metadata.get("data_watermark", {}) if isinstance(metadata, dict) else {}
    rate_limited = bool(rate_limit.get("rate_limited"))
    exact_status = "COMPLETE"
    if rate_limited:
        exact_status = str(rate_limit.get("status") or RATE_LIMITED_PARTIAL)
    elif handoff_summary.get("exact_ticker_not_refreshed_rows"):
        exact_status = "EXACT_TICKER_NOT_REFRESHED"
    elif not handoff_rows:
        exact_status = "NO_POSITIVE_EV_ROWS"
    book_status = "COMPLETE"
    if not handoff_rows:
        book_status = "NO_POSITIVE_EV_ROWS"
    elif any(not row.get("book_orderbook_fresh") for row in handoff_rows):
        book_status = "BOOK_STALE_OR_MISSING"
    url_status = "COMPLETE"
    if audit.get("summary", {}).get("current_verified_links", 0) < audit.get("summary", {}).get("positive_ev_rows", 0):
        url_status = "URL_VERIFICATION_INCOMPLETE"
    return {
        "market_data_top_strip": {
            "status": top_strip.get("market_data_state") or top_strip.get("status") or "UNKNOWN_NO_TOP_STRIP_ARTIFACT",
            "source": str(reports_dir / "phase_3ak" / "top_strip_status.json"),
            "generated_at": top_strip.get("generated_at"),
        },
        "generic_market_snapshot": {
            "status": "WATERMARK_REPORTED" if watermark else "UNKNOWN",
            "latest_market_last_seen_at": watermark.get("latest_market_last_seen_at"),
            "latest_snapshot_captured_at": watermark.get("latest_snapshot_captured_at"),
        },
        "exact_opportunity_catalog": {
            "status": exact_status,
            "data_complete": bool(handoff_summary.get("data_complete")),
            "rows_checked": handoff_summary.get("exact_catalog_rows_checked", 0),
            "fresh_rows": handoff_summary.get("exact_catalog_fresh_rows", 0),
            "not_refreshed_rows": handoff_summary.get("exact_ticker_not_refreshed_rows", 0),
        },
        "book_orderbook": {
            "status": book_status,
            "fresh_rows": handoff_summary.get("book_fresh_rows", 0),
            "executable_rows": handoff_summary.get("book_executable_rows", 0),
        },
        "url_verification": {
            "status": url_status,
            "verified_rows": handoff_summary.get("url_verified_rows", 0),
            "positive_ev_rows": audit.get("summary", {}).get("positive_ev_rows", 0),
        },
    }


def _phase3ar_read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _phase3ar_top_rate_limit_endpoint(rate_limit: dict[str, Any]) -> str | None:
    endpoints = rate_limit.get("endpoints") if isinstance(rate_limit, dict) else []
    if not isinstance(endpoints, list) or not endpoints:
        return None
    first = endpoints[0]
    if not isinstance(first, dict):
        return None
    return str(first.get("endpoint") or "") or None


def _phase3ar_catalog_refresh_next_action(
    status: str,
    candidate_count: int,
    refreshed_count: int,
    *,
    exact_ticker_not_refreshed_count: int = 0,
    rate_limited: bool = False,
) -> str:
    if rate_limited or status in PHASE3AR_RATE_LIMIT_STATUSES:
        return (
            "Stop condition: RATE_LIMITED_KALSHI_API left exact catalog data partial. "
            "Wait for the Kalshi backoff window, then rerun the bounded exact refresh command."
        )
    if status == "BLOCKED_BY_ACTIVE_WRITER":
        return "Stop condition: active DB writer detected. Wait for writer to clear, then rerun dry-run."
    if status == "DRY_RUN" and candidate_count:
        return (
            "Run kalshi-bot phase3ar-refresh-catalog-for-opportunities "
            "--apply-readonly-refresh --max-markets 100 --max-duration-seconds 120 "
            "--output-dir reports/phase3ar --reports-dir reports."
        )
    if status.startswith("READONLY_REFRESH") and refreshed_count:
        return "Rerun kalshi-bot phase3ar-url-audit --output-dir reports/phase3ar --reports-dir reports."
    if status == "NO_REFRESH_CANDIDATES":
        if exact_ticker_not_refreshed_count:
            return (
                "Stop condition: exact positive-EV catalog rows remain stale or missing, "
                "but no safe exact refresh candidate was available. Inspect catalog_refresh_plan.json."
            )
        return "Stop condition: no exact active stale catalog candidates to refresh."
    return "Review catalog_refresh_plan.json before continuing."


def _phase3ar_db_writer_status(*, settings: Settings) -> dict[str, Any]:
    try:
        return db_writer_monitor(settings=settings)
    except Exception as exc:  # noqa: BLE001 - report must terminate.
        return {
            "status": "UNKNOWN_REQUIRES_INVESTIGATION",
            "safe_to_start_write": False,
            "error": str(exc),
        }


def _phase3ar_write_url_backup(output_dir: Path, rows: list[dict[str, Any]], *, run_id: str) -> Path:
    backup_dir = output_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"{run_id}_url_repair_backup.json"
    path.write_text(json.dumps(rows, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path


def _phase3ar_settlement_noise_row(row: dict[str, Any]) -> dict[str, Any]:
    code = str(row.get("specific_reason_code") or "")
    mapping = {
        "SETTLEMENT_RULE_MISSING": "settlement terms unknown",
        "SETTLEMENT_SOURCE_MISSING": "final outcome missing for resolution only",
        "MARKET_CLOSE_UNKNOWN": "settlement terms unknown",
        "MARKET_SETTLEMENT_STATUS_UNKNOWN": "unknown",
        "OPEN_MARKET_SETTLEMENT_TERMS_KNOWN": "open market settlement terms known",
        "MARKET_NOT_SETTLEABLE_YET": "open market settlement terms known",
        "MARKET_ALREADY_SETTLED_BUT_OUTCOME_MISSING": "final outcome missing for resolution only",
        "SYNTHETIC_MARKET_NO_SETTLEMENT_RULE": "synthetic/composite unsupported",
        "COMPOSITE_MARKET_REQUIRES_RESOLVER": "synthetic/composite unsupported",
        "GENERAL_SOURCE_NOT_FORECAST_SAFE": "source not forecast safe",
        "SPORTS_PLACEHOLDER_BLOCKED": "placeholder/provenance blocked",
        "PARTIAL_PROVENANCE_BLOCKED": "placeholder/provenance blocked",
        "KALSHI_CATALOG_STALE": "catalog stale",
    }
    payload = dict(row)
    payload["noise_class"] = mapping.get(code, "unknown")
    return payload


def _phase3ar_metadata(
    session: Session,
    *,
    settings: Settings,
    output_dir: Path,
    command: str,
    command_args: dict[str, Any],
) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    redacted_db_url = redact_database_url(db_url)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AR",
        "phase_version": PHASE_3AR_LINK_REPAIR_VERSION,
        "repository_root": str(Path.cwd().resolve()),
        "git_branch": _phase3ar_git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "git_commit": _phase3ar_git_value("rev-parse", "HEAD"),
        "git_dirty": _phase3ar_git_dirty(),
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(__file__).resolve()),
        "resolved_database_url": redacted_db_url,
        "database_fingerprint": {
            "url": redacted_db_url,
            "location": describe_db_location(db_url),
        },
        "command_arguments": {"command": command, **command_args},
        "output_dir": str(output_dir),
        "data_watermark": _phase3ar_data_watermark(session),
        "safety_flags": {
            "paper_only": True,
            "ui_read_only": settings.ui_read_only,
            "execution_enabled": settings.execution_enabled,
            "execution_dry_run": settings.execution_dry_run,
            "live_or_demo_execution": False,
            "order_submission": False,
            "paper_trade_creation": False,
            "fake_links_created": False,
            "sibling_or_fuzzy_matching_allowed": False,
        },
    }


def _phase3ar_data_watermark(session: Session) -> dict[str, Any]:
    latest_market = session.scalar(select(func.max(Market.last_seen_at)))
    latest_ranking = session.scalar(select(func.max(MarketRanking.ranked_at)))
    latest_snapshot = session.scalar(select(func.max(MarketSnapshot.captured_at)))
    return {
        "latest_market_last_seen_at": latest_market.isoformat() if latest_market else None,
        "latest_ranking_ranked_at": latest_ranking.isoformat() if latest_ranking else None,
        "latest_snapshot_captured_at": latest_snapshot.isoformat() if latest_snapshot else None,
    }


def _phase3ar_git_value(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _phase3ar_git_dirty() -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _phase3ar_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _phase3ar_write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "market_ticker",
        "market_title",
        "url_status",
        "specific_malformed_reason",
        "book_status",
        "primary_blocker",
        "raw_ev",
        "next_action",
        "kalshi_url",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _phase3ar_write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_phase3ar_url_audit_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AR URL Audit",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        "- Live/demo execution: blocked",
        "- Order submission/cancel/replace: blocked",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {json.dumps(value, sort_keys=True) if isinstance(value, dict) else value}")
    lines.extend(["", "## Next Action", "", payload["next_action"], ""])
    return "\n".join(lines)


def _render_phase3ar_url_repair_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Phase 3AR URL Repair",
            "",
            f"- Generated at: {payload['generated_at']}",
            f"- Status: {payload['status']}",
            f"- Dry run: {payload['dry_run']}",
            f"- Apply: {payload['apply']}",
            f"- Repaired rows: {payload['summary']['repaired_rows']}",
            f"- URL/catalog metadata writes: {payload['url_catalog_metadata_writes']}",
            "",
            "## Next Action",
            "",
            payload["next_action"],
            "",
        ]
    )


def _render_phase3ar_catalog_stale_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AR Catalog Stale Diagnostic",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        "- Live/demo execution: blocked",
        "- Order submission/cancel/replace: blocked",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {json.dumps(value, sort_keys=True) if isinstance(value, dict) else value}")
    lines.extend(["", "## Stale Rows", ""])
    for row in payload.get("rows", [])[:25]:
        lines.append(
            f"- {row['market_ticker']}: {row['stale_reason']} "
            f"(last_seen={row.get('catalog_last_seen_at')}, age={row.get('stale_age_seconds')}s)"
        )
    if not payload.get("rows"):
        lines.append("- none")
    lines.extend(["", "## Next Action", "", payload["next_action"], ""])
    return "\n".join(lines)


def _render_phase3ar_catalog_refresh_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    rate_limit = payload.get("rate_limit", {})
    views = payload.get("freshness_views", {})
    lines = [
        "# Phase 3AR Catalog Refresh Plan",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Status: {payload['status']}",
        f"- Dry run: {payload['dry_run']}",
        f"- Apply read-only refresh: {payload['apply_readonly_refresh']}",
        f"- Catalog metadata writes: {payload['catalog_metadata_writes']}",
        f"- Refresh candidates: {summary['refresh_candidates']}",
        f"- Refreshed rows: {summary['refreshed_rows']}",
        f"- Failed rows: {summary['failed_rows']}",
        f"- Exact positive-EV tickers: {summary.get('exact_positive_ev_tickers', 0)}",
        f"- Exact catalog fresh rows: {summary.get('exact_catalog_fresh_rows', 0)}",
        f"- Exact ticker not refreshed rows: {summary.get('exact_ticker_not_refreshed_rows', 0)}",
        f"- Data completeness: {summary.get('data_completeness', 'unknown')}",
        "",
        "## Kalshi API Rate Limit",
        "",
        f"- Status: {rate_limit.get('status', 'COMPLETE')}",
        f"- Endpoint: {rate_limit.get('top_endpoint') or _phase3ar_top_rate_limit_endpoint(rate_limit)}",
        f"- Retry count: {rate_limit.get('retry_count', 0)}",
        f"- Total sleep seconds: {rate_limit.get('total_sleep_seconds', 0)}",
        f"- Rows fetched before limit: {rate_limit.get('rows_fetched_before_limit', 0)}",
        f"- Data completeness: {rate_limit.get('data_completeness', 'complete')}",
        "",
        "## Freshness Views",
        "",
    ]
    for name, view in views.items():
        if isinstance(view, dict):
            lines.append(f"- {name}: {json.dumps(view, sort_keys=True, default=str)}")
    lines.extend(
        [
            "",
            "## Exact Handoff Rows",
            "",
            "| Ticker | Refresh status | Catalog fresh | URL status | Book status | Last seen |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload.get("exact_catalog_handoff_rows", [])[:25]:
        lines.append(
            f"| {row.get('market_ticker')} | {row.get('refresh_status')} | "
            f"{row.get('exact_catalog_fresh')} | {row.get('url_verification_status')} | "
            f"{row.get('book_orderbook_status')} | {row.get('catalog_last_seen_at')} |"
        )
    if not payload.get("exact_catalog_handoff_rows"):
        lines.append("| n/a | no positive-EV rows | n/a | n/a | n/a | n/a |")
    lines.extend(
        [
            "",
            "## Stop Conditions",
            "",
            "- Stop if status is BLOCKED_BY_ACTIVE_WRITER.",
            "- Stop if status starts with RATE_LIMITED_; exact opportunity data is partial.",
            "- Stop if exact ticker rows remain EXACT_TICKER_NOT_REFRESHED.",
            "- After a complete read-only refresh, rerun phase3ar-url-audit before URL repair.",
            "",
            "## Next Action",
            "",
            payload["next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def _render_phase3ar_book_refresh_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    return "\n".join(
        [
            "# Phase 3AR Book Refresh For Verified Links",
            "",
            f"- Status: {payload.get('status')}",
            f"- Verified links: {summary.get('verified_tradeable_links', 0)}",
            f"- Book refresh candidates: {summary.get('book_refresh_needed_rows', 0)}",
            f"- Market-data writes: {payload.get('market_data_writes')}",
            "",
        ]
    )


def _render_phase3ar_settlement_noise_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Phase 3AR Settlement Check Noise Audit",
            "",
            f"- Generated at: {payload['generated_at']}",
            f"- Generic remaining: {payload['summary']['generic_settlement_check_failed_remaining']}",
            f"- Noise counts: {json.dumps(payload.get('noise_class_counts', {}), sort_keys=True)}",
            "",
        ]
    )


def _render_phase3ar_executive_summary(
    audit: dict[str, Any],
    catalog_stale: dict[str, Any],
    catalog_refresh: dict[str, Any],
    dry_run: dict[str, Any],
    book: dict[str, Any],
    gate: dict[str, Any],
) -> str:
    audit_summary = audit["summary"]
    stale_summary = catalog_stale["summary"]
    refresh_summary = catalog_refresh["summary"]
    gate_summary = gate["summary"]
    book_summary = book.get("summary", {})
    malformed_counts = audit_summary.get("specific_malformed_reason_counts") or audit_summary.get("previous_malformed_reason_counts")
    stale_tickers = [row["market_ticker"] for row in catalog_stale.get("rows", [])]
    lines = [
        "# Phase 3AR Link Repair Report",
        "",
        (
            "1. Why are links still unverified? Exact URL building is blocked by "
            f"stale catalog evidence for {stale_summary['stale_catalog_rows']} row(s); "
            "Phase 3AR did not persist URLs from stale catalog rows."
        ),
        f"2. Rows blocked by stale catalog: {stale_summary['stale_catalog_rows']}.",
        f"3. Exact stale catalog records: {', '.join(stale_tickers[:25]) if stale_tickers else 'none'}.",
        (
            "4. Can they be refreshed safely? "
            f"{'yes' if refresh_summary['refresh_candidates'] else 'no'}; "
            f"refresh candidates: {refresh_summary['refresh_candidates']}."
        ),
        f"5. URLs repairable after current dry-run: {dry_run['summary']['safe_to_persist']}.",
        f"6. Book-refresh candidates after URL verification: {book_summary.get('book_refresh_needed_rows', 0)}.",
        f"7. Next exact command: {_phase3ar_best_next_command(audit, catalog_stale, catalog_refresh, dry_run, book)}.",
        "8. Paper trades created: no; live/demo exchange writes: no.",
        "",
        f"- Positive-EV rows: {audit_summary['positive_ev_rows']}.",
        f"- Exact catalog matches: {audit_summary['exact_catalog_matches']}.",
        f"- Current malformed URLs: {audit_summary['current_malformed_urls']}; malformed reason counts: {json.dumps(malformed_counts, sort_keys=True)}.",
        f"- Current verified links: {audit_summary['current_verified_links']}.",
        f"- Paper-ready rows: {gate_summary.get('paper_ready_rows', 0)}.",
        f"- Primary blocker counts: {json.dumps(gate_summary.get('primary_blocker_counts', {}), sort_keys=True)}.",
        f"- Git commit: {audit.get('git_commit') or 'unknown'}.",
        f"- Database fingerprint: {json.dumps(audit.get('database_fingerprint', {}), sort_keys=True)}.",
        f"- Command args: {json.dumps(audit.get('command_arguments', {}), sort_keys=True)}.",
        f"- Data watermark: {json.dumps(audit.get('data_watermark', {}), sort_keys=True)}.",
        f"- Safety flags: {json.dumps(audit.get('safety_flags', {}), sort_keys=True)}.",
        "",
    ]
    return "\n".join(lines)


def _render_phase3ar_next_actions(
    audit: dict[str, Any],
    catalog_stale: dict[str, Any],
    catalog_refresh: dict[str, Any],
    dry_run: dict[str, Any],
    book: dict[str, Any],
    gate: dict[str, Any],
) -> str:
    next_command = _phase3ar_best_next_command(audit, catalog_stale, catalog_refresh, dry_run, book)
    return "\n".join(
        [
            "# Phase 3AR Next Actions",
            "",
            "All Phase 3AR command lines below were checked against CLI help.",
            "",
            f"1. {next_command}",
            (
                "2. Stop condition: if the refresh command reports "
                "BLOCKED_BY_ACTIVE_WRITER, wait for the writer to clear and rerun dry-run."
            ),
            "3. After catalog refresh, rerun `kalshi-bot phase3ar-url-audit --output-dir reports/phase3ar --reports-dir reports`.",
            "4. Keep malformed/unverified/synthetic/composite rows diagnostic-only.",
            "5. Do not force paper trades; let the canonical paper-ready gate advance rows naturally.",
            "",
        ]
    )


def _phase3ar_best_next_command(
    audit: dict[str, Any],
    catalog_stale: dict[str, Any],
    catalog_refresh: dict[str, Any],
    dry_run: dict[str, Any],
    book: dict[str, Any],
) -> str:
    refresh_summary = catalog_refresh.get("summary", {})
    rate_limit = catalog_refresh.get("rate_limit", {})
    if isinstance(rate_limit, dict) and rate_limit.get("rate_limited"):
        return "kalshi-bot phase3ar-refresh-catalog-for-opportunities --apply-readonly-refresh --max-markets 100 --max-duration-seconds 120 --output-dir reports/phase3ar --reports-dir reports"
    if isinstance(refresh_summary, dict) and refresh_summary.get("exact_ticker_not_refreshed_rows"):
        return "kalshi-bot phase3ar-refresh-catalog-for-opportunities --apply-readonly-refresh --max-markets 100 --max-duration-seconds 120 --output-dir reports/phase3ar --reports-dir reports"
    if dry_run["summary"]["safe_to_persist"]:
        return "kalshi-bot phase3ar-url-repair --apply --backup-first --max-records 100 --output-dir reports/phase3ar --reports-dir reports"
    if catalog_refresh.get("summary", {}).get("refresh_candidates"):
        return "kalshi-bot phase3ar-refresh-catalog-for-opportunities --apply-readonly-refresh --max-markets 100 --max-duration-seconds 120 --output-dir reports/phase3ar --reports-dir reports"
    if catalog_stale.get("summary", {}).get("stale_catalog_rows"):
        return "kalshi-bot phase3ar-catalog-stale-diagnostic --output-dir reports/phase3ar --reports-dir reports"
    if book.get("summary", {}).get("book_refresh_needed_rows"):
        return "kalshi-bot phase3ar-refresh-books-for-verified-links --apply-readonly-refresh --max-markets 100 --max-duration-seconds 120 --output-dir reports/phase3ar"
    if audit["summary"]["current_verified_links"]:
        return "kalshi-bot phase3ar-link-repair-report --output-dir reports/phase3ar --reports-dir reports"
    return "kalshi-bot phase3ar-url-audit --output-dir reports/phase3ar --reports-dir reports"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AR Crypto Forecast Coverage Repair",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: blocked; this phase uses public reads and local writes only.",
        f"- Diagnostic scope: {payload.get('diagnostic_scope', {}).get('scope', 'UNKNOWN')}",
        f"- Diagnostic ticker count: {payload.get('diagnostic_scope', {}).get('ticker_count', 0)}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Status Counts", ""])
    for key, value in payload["status_counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Repair Result",
            "",
            f"- attempted: {payload['repair_result']['attempted']}",
            f"- repaired: {payload['repair_result']['repaired']}",
            f"- still_missing: {payload['repair_result']['still_missing']}",
            "",
            "## Top Blocked Rows",
            "",
            "| Ticker | Status | Snapshot | Components | Last skip | Next action |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["top_blocked"][:25]:
        components = ",".join(row["component_symbols"]) or "n/a"
        lines.append(
            f"| {row['ticker']} | {row['status']} | {row['snapshot_at'] or 'none'} | "
            f"{components} | {row['latest_skip_reason'] or 'none'} | {row['next_action']} |"
        )
    if not payload["top_blocked"]:
        lines.append("| n/a | all ready | n/a | n/a | n/a | Run crypto_v2 forecasts. |")
    lines.extend(["", "## Next Commands", "", "```bash"])
    lines.extend(payload["next_commands"])
    lines.extend(
        [
            "```",
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)
