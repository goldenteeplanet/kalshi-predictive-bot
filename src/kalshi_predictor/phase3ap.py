from __future__ import annotations

import json
import csv
import hashlib
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import database_url_from_settings, redact_database_url
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.locks import sqlite_lock_diagnostics
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    PositionSizingDecisionLog,
    Settlement,
)
from kalshi_predictor.kalshi.orderbook import parse_orderbook, usable_bid_ask_book
from kalshi_predictor.learning.safety import learning_status
from kalshi_predictor.opportunities.market_identity import (
    BUILT_FROM_EXACT_CATALOG,
    COMPOSITE_LOCAL_ONLY,
    GENERAL_SOURCE_NOT_SAFE,
    MALFORMED_URL,
    CATALOG_STALE,
    MARKET_NOT_IN_CATALOG,
    PARTIAL_PROVENANCE_BLOCKED,
    PLACEHOLDER_BLOCKED,
    CATALOG_MATCH_MISSING,
    STALE_CATALOG,
    SYNTHETIC_ONLY,
    market_identity_fields,
    verify_market_identity,
)
from kalshi_predictor.opportunities.window_eligibility import (
    EXPIRED_WINDOW_EXCLUDED,
    MARKET_CLOSED_OR_SETTLED,
    MARKET_CLOSE_TOO_NEAR,
    MARKET_NOT_OPEN,
    current_market_window_status,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.phase3aa import build_settlement_eta_schedule
from kalshi_predictor.phase3al import build_phase3al_resume_plan
from kalshi_predictor.phase3aj_gap_closure import build_paper_trade_funnel
from kalshi_predictor.phase3ak import write_market_data_refresh_status
from kalshi_predictor.phase3an import (
    build_phase3an_crypto_feature_completeness,
    build_phase3an_economic_news_watch,
    build_phase3an_general_sources_status,
    build_phase3an_paper_funnel_explain,
    build_phase3an_settlement_health_confirm,
    build_phase3an_sports_blocker_report,
    _phase3an_data_watermark,
    _phase3an_safety_flags,
)
from kalshi_predictor.phase3ar import build_crypto_forecast_coverage
from kalshi_predictor.phase3as import build_active_market_universe
from kalshi_predictor.phase3at import build_active_crypto_router
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE_3AP_VERSION = "phase3ap_v1"
PHASE_3AP_UNBLOCK_VERSION = "phase3ap_executable_unblocker_v1"
DEFAULT_PHASE3AP_SCAN_LIMIT = 500
QUOTE_STALE_AFTER_MINUTES = Decimal("15")
MIN_EXECUTABLE_LIQUIDITY_SCORE = Decimal("25")
RAW_EV_COST_BUFFER = Decimal("0")

BOOK_REASON_CODES = (
    "NO_ORDERBOOK_SNAPSHOT",
    "EMPTY_ORDERBOOK",
    "STALE_ORDERBOOK",
    "WIDE_SPREAD",
    "ZERO_VISIBLE_DEPTH",
    "INSUFFICIENT_DEPTH",
    "INVALID_YES_NO_BOOK",
    "CROSSED_OR_INCONSISTENT_BOOK",
    "PRICE_OUTSIDE_VALID_RANGE",
    "MARKET_NOT_OPEN",
    "MARKET_CLOSED_OR_SETTLED",
    "EXPIRED_WINDOW_EXCLUDED",
    "MARKET_PAUSED",
    "MARKET_CLOSE_TOO_NEAR",
    "BUILT_FROM_EXACT_CATALOG",
    "UNVERIFIED_KALSHI_LINK",
    "SYNTHETIC_OR_COMPOSITE_ONLY",
    "UNKNOWN_REQUIRES_INVESTIGATION",
)

SETTLEMENT_REASON_CODES = (
    "SETTLEMENT_RULE_MISSING",
    "SETTLEMENT_SOURCE_MISSING",
    "MARKET_CLOSE_UNKNOWN",
    "MARKET_SETTLEMENT_STATUS_UNKNOWN",
    "MARKET_NOT_SETTLEABLE_YET",
    "MARKET_ALREADY_SETTLED_BUT_OUTCOME_MISSING",
    "SYNTHETIC_MARKET_NO_SETTLEMENT_RULE",
    "COMPOSITE_MARKET_REQUIRES_RESOLVER",
    "GENERAL_SOURCE_NOT_FORECAST_SAFE",
    "SPORTS_PLACEHOLDER_BLOCKED",
    "PARTIAL_PROVENANCE_BLOCKED",
    "ECONOMIC_NEWS_NO_COMPATIBLE_MARKET",
    "KALSHI_CATALOG_STALE",
    "UNKNOWN_REQUIRES_INVESTIGATION",
)

PAPER_READY_REASON_HIERARCHY = (
    "MARKET_CLOSED_OR_SETTLED",
    "EXPIRED_WINDOW_EXCLUDED",
    "MARKET_CLOSE_TOO_NEAR",
    "STALE_CATALOG",
    "BUILT_FROM_EXACT_CATALOG",
    "UNVERIFIED_KALSHI_LINK",
    "MALFORMED_URL",
    "CATALOG_MATCH_MISSING",
    "MARKET_NOT_OPEN",
    "STALE_QUOTE",
    "NO_ORDERBOOK_SNAPSHOT",
    "EMPTY_ORDERBOOK",
    "ZERO_VISIBLE_DEPTH",
    "SPREAD_TOO_WIDE",
    "LIQUIDITY_TOO_LOW",
    "NO_POSITIVE_EXECUTABLE_EV",
    "CONFIDENCE_BELOW_THRESHOLD",
    "SETTLEMENT_TERMS_UNKNOWN",
    "SOURCE_NOT_FORECAST_SAFE",
    "PHASE_3S_SKIP",
    "PHASE_3M_ZERO_SIZE",
    "PHASE_3N_RISK_BLOCK",
    "DUPLICATE_IDEMPOTENCY_KEY",
    "UNKNOWN_REQUIRES_INVESTIGATION",
)


@dataclass(frozen=True)
class Phase3APArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    script_path: Path


@dataclass(frozen=True)
class Phase3APDiagnosticArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class Phase3APUnblockArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    next_actions_path: Path
    book_json_path: Path
    book_markdown_path: Path
    settlement_json_path: Path
    settlement_markdown_path: Path
    paper_ready_gate_path: Path
    positive_ev_csv_path: Path
    no_executable_book_csv_path: Path
    settlement_blocked_csv_path: Path
    manifest_path: Path


def build_phase3ap_safe_night_runner_plan(
    session: Session,
    *,
    settings: Settings | None = None,
    max_cycles: int = 32,
    interval_minutes: int = 15,
    scan_limit: int = 500,
) -> dict[str, Any]:
    """Build a paper-only v2 overnight plan with database-lock and learning gates."""
    resolved = settings or get_settings()
    session.flush()
    learning = learning_status(session, settings=resolved)
    resume = build_phase3al_resume_plan(
        session,
        settings=resolved,
        limit=scan_limit,
    )
    settlement = build_settlement_eta_schedule(session, limit=scan_limit)
    crypto = build_phase3an_crypto_feature_completeness(session, settings=resolved)
    active_universe = build_active_market_universe(session, limit=scan_limit)
    crypto_coverage = build_crypto_forecast_coverage(
        session,
        settings=resolved,
        limit=scan_limit,
    )
    crypto_router = build_active_crypto_router(
        session,
        settings=resolved,
        limit=scan_limit,
    )
    lock_diag = sqlite_lock_diagnostics(settings=resolved)
    blockers = _blockers(resolved, resume, lock_diag)
    steps = _steps(resume, crypto, crypto_coverage)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AP",
        "phase_version": PHASE_3AP_VERSION,
        "mode": "PAPER_ONLY_SAFE_NIGHT_RUNNER_V2",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "runner_defaults": {
            "max_cycles": max_cycles,
            "interval_minutes": interval_minutes,
            "scan_limit": scan_limit,
            "paper_only": True,
            "demo_execution": "blocked",
            "live_execution": "blocked",
        },
        "status": "BLOCKED" if blockers else "READY",
        "blockers": blockers,
        "learning_status": learning,
        "resume_plan": resume["resume_decision"],
        "settlement_summary": settlement["summary"],
        "crypto_summary": crypto["summary"],
        "active_universe_summary": active_universe["summary"],
        "crypto_forecast_coverage_summary": crypto_coverage["summary"],
        "crypto_active_router_summary": crypto_router["summary"],
        "database_lock_diagnostics": lock_diag,
        "cycle_steps": steps,
        "script": _script(max_cycles=max_cycles, interval_minutes=interval_minutes),
        "recommended_next_action": _next_action(blockers),
    }


def write_phase3ap_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ap"),
    settings: Settings | None = None,
    max_cycles: int = 32,
    interval_minutes: int = 15,
    scan_limit: int = 500,
) -> Phase3APArtifactSet:
    payload = build_phase3ap_safe_night_runner_plan(
        session,
        settings=settings,
        max_cycles=max_cycles,
        interval_minutes=interval_minutes,
        scan_limit=scan_limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ap_safe_night_runner_v2.json"
    markdown_path = output_dir / "phase3ap_safe_night_runner_v2.md"
    script_path = output_dir / "safe_night_runner_v2.sh"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    script_path.write_text(payload["script"], encoding="utf-8")
    return Phase3APArtifactSet(output_dir, json_path, markdown_path, script_path)


def _blockers(
    settings: Settings,
    resume: dict[str, Any],
    lock_diag: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if settings.execution_enabled:
        blockers.append("execution_enabled_true")
    if not settings.learning_block_demo_execution:
        blockers.append("demo_execution_not_blocked")
    if not settings.learning_block_live_execution:
        blockers.append("live_execution_not_blocked")
    if not resume["resume_decision"]["can_resume_now"]:
        blockers.extend(resume["resume_decision"]["blockers"])
    if not lock_diag.get("safe_to_write", True):
        blockers.append("database_write_lock_active")
    return sorted(set(blockers))


def _steps(
    resume: dict[str, Any],
    crypto: dict[str, Any],
    crypto_coverage: dict[str, Any],
) -> list[dict[str, Any]]:
    learning_allowed = bool(resume["resume_decision"]["can_resume_now"])
    return [
        _step("db-locks", "kalshi-bot db-locks", True),
        _step("collect", "kalshi-bot collect-once --status open --limit 500 --max-pages 5", True),
        _step("market-legs", "kalshi-bot market-legs-parse --refresh", True),
        _step(
            "link-coverage",
            "kalshi-bot link-coverage --output reports/link_coverage_report.md",
            True,
        ),
        _step(
            "crypto-ingest",
            "kalshi-bot ingest-crypto --symbols BTC,ETH,SOL,XRP,DOGE --source coinbase",
            True,
        ),
        _step(
            "crypto-build-features",
            "kalshi-bot build-crypto-features --symbols BTC,ETH,SOL,XRP,DOGE",
            True,
        ),
        _step(
            "crypto-history-warmup",
            "kalshi-bot crypto-history-warmup --symbols BTC,ETH,SOL,XRP,DOGE",
            True,
        ),
        _step("crypto-link", "kalshi-bot link-crypto-markets", True),
        _step(
            "active-universe",
            "kalshi-bot active-universe-doctor --mark-deprecated --output-dir reports/phase3as",
            True,
        ),
        _step("crypto-features", "kalshi-bot phase3an-crypto-feature-completeness", True),
        _step(
            "crypto-forecast-coverage",
            "kalshi-bot crypto-forecast-doctor --repair-snapshots --output-dir reports/phase3ar",
            True,
        ),
        _step(
            "crypto-active-router",
            "kalshi-bot phase3at-active-router --output-dir reports/phase3at",
            True,
        ),
        _step(
            "crypto-v2",
            "kalshi-bot forecast --model crypto_v2",
            bool(crypto_coverage["summary"]["ready_to_forecast"])
            or bool(crypto["summary"]["can_rerun_crypto_v2"]),
        ),
        _step("forecast", "kalshi-bot forecast --model all", True),
        _step("settlements", "kalshi-bot phase3aa-realize --sync-settlements --dry-run", True),
        _step(
            "learning",
            "LEARNING_MODE=true EXECUTION_ENABLED=false kalshi-bot learning-once",
            learning_allowed,
        ),
        _step("reports", "kalshi-bot phase3ap-night-runner-v2", True),
        _step("paper-summary", "kalshi-bot paper-summary", True),
    ]


def _step(name: str, command: str, enabled: bool) -> dict[str, Any]:
    return {"name": name, "command": command, "enabled": enabled}


def _script(*, max_cycles: int, interval_minutes: int) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -u",
            "export LEARNING_MODE=true",
            "export EXECUTION_ENABLED=false",
            "export LEARNING_BLOCK_DEMO_EXECUTION=true",
            "export LEARNING_BLOCK_LIVE_EXECUTION=true",
            f"MAX_CYCLES=${{MAX_CYCLES:-{max_cycles}}}",
            f"INTERVAL_MINUTES=${{INTERVAL_MINUTES:-{interval_minutes}}}",
            'echo "Phase 3AP safe night runner v2: paper only"',
            'for cycle in $(seq 1 "$MAX_CYCLES"); do',
            '  echo "cycle $cycle / $MAX_CYCLES"',
            "  kalshi-bot db-locks || true",
            "  kalshi-bot collect-once --status open --limit 500 --max-pages 5 || true",
            "  kalshi-bot market-legs-parse --refresh || true",
            "  kalshi-bot link-coverage --output reports/link_coverage_report.md || true",
            (
                "  kalshi-bot ingest-crypto --symbols BTC,ETH,SOL,XRP,DOGE "
                "--source coinbase || true"
            ),
            "  kalshi-bot build-crypto-features --symbols BTC,ETH,SOL,XRP,DOGE || true",
            (
                "  kalshi-bot crypto-history-warmup --symbols BTC,ETH,SOL,XRP,DOGE "
                "--output-dir reports/phase3at || true"
            ),
            "  kalshi-bot link-crypto-markets || true",
            (
                "  kalshi-bot active-universe-doctor --mark-deprecated "
                "--output-dir reports/phase3as || true"
            ),
            "  kalshi-bot phase3an-crypto-feature-completeness || true",
            (
                "  kalshi-bot crypto-forecast-doctor --repair-snapshots "
                "--output-dir reports/phase3ar || true"
            ),
            "  kalshi-bot forecast --model crypto_v2 || true",
            "  kalshi-bot phase3at-active-router --output-dir reports/phase3at || true",
            "  kalshi-bot forecast --model all || true",
            "  kalshi-bot phase3aa-realize --sync-settlements --dry-run || true",
            "  kalshi-bot phase3al-learning-resume --output-dir reports/phase3al || true",
            "  CAN_LEARN=$(python - <<'PY'",
            "import json",
            "from pathlib import Path",
            "path = Path('reports/phase3al/phase3al_learning_resume.json')",
            "allowed = False",
            "if path.exists():",
            "    data = json.loads(path.read_text())",
            "    allowed = bool(data.get('resume_decision', {}).get('can_resume_now'))",
            "print('true' if allowed else 'false')",
            "PY",
            "  )",
            '  if [ "$CAN_LEARN" = "true" ]; then',
            "    LEARNING_MODE=true EXECUTION_ENABLED=false kalshi-bot learning-once || true",
            "  else",
            '    echo "learning skipped: Phase 3AL resume gate is closed"',
            "  fi",
            "  kalshi-bot paper-summary || true",
            '  sleep "$((INTERVAL_MINUTES * 60))"',
            "done",
            "kalshi-bot phase3ap-night-runner-v2 || true",
            "",
        ]
    )


def _next_action(blockers: list[str]) -> str:
    if blockers:
        return "Review blockers before starting the v2 runner; keep learning paused if needed."
    return "Use the generated safe_night_runner_v2.sh script for a paper-only overnight run."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AP Automated Safe Night Runner v2",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Status: {payload['status']}",
        "",
        "## Blockers",
        "",
    ]
    if payload["blockers"]:
        lines.extend(f"- {blocker}" for blocker in payload["blockers"])
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Cycle Steps",
            "",
            "| Step | Enabled | Command |",
            "| --- | --- | --- |",
        ]
    )
    for step in payload["cycle_steps"]:
        lines.append(
            f"| {step['name']} | {step['enabled']} | `{step['command']}` |"
        )
    lines.extend(
        [
            "",
            "## Runner Script",
            "",
            "```bash",
            payload["script"],
            "```",
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def build_phase3ap_book_diagnostic(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ap"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    rows = _phase3ap_gate_rows(
        session,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    all_positive_rows = [row for row in rows if _phase3ap_positive_ev(row)]
    positive_rows = [row for row in all_positive_rows if row.get("current_positive_ev_eligible")]
    expired_positive_rows = [
        row for row in all_positive_rows if row.get("window_status") == EXPIRED_WINDOW_EXCLUDED
    ]
    finalized_rows = [
        row for row in all_positive_rows if row.get("window_status") == MARKET_CLOSED_OR_SETTLED
    ]
    no_book_rows = [row for row in positive_rows if not row["executable_book"]]
    metadata = _phase3ap_metadata(
        session,
        settings=resolved,
        output_dir=output_dir,
        command="kalshi-bot phase3ap-book-diagnostic",
        command_args={
            "output_dir": str(output_dir),
            "window_hours": window_hours,
            "limit": limit,
        },
    )
    return {
        "generated_at": metadata["generated_at"],
        "phase": "3AP",
        "phase_version": PHASE_3AP_UNBLOCK_VERSION,
        "mode": "PAPER_READ_ONLY_EXECUTABLE_BOOK_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "thresholds": _phase3ap_thresholds(resolved),
        "summary": {
            "rankings_scanned": len(rows),
            "positive_ev_rows": len(positive_rows),
            "current_positive_ev_rows": len(positive_rows),
            "all_positive_ev_rows_including_diagnostics": len(all_positive_rows),
            "expired_positive_ev_rows": len(expired_positive_rows),
            "expired_excluded_rows": len(expired_positive_rows),
            "historical_diagnostic_rows": sum(1 for row in rows if row.get("diagnostic_only")),
            "finalized_or_settled_rows": len(finalized_rows),
            "stale_catalog_rows": sum(
                1 for row in positive_rows if row.get("primary_blocker") == STALE_CATALOG
            ),
            "stale_quote_rows": sum(
                1 for row in positive_rows if row.get("primary_blocker") == "STALE_QUOTE"
            ),
            "positive_ev_no_executable_book_rows": len(no_book_rows),
            "positive_ev_executable_book_rows": len(positive_rows) - len(no_book_rows),
            "paper_ready_rows": sum(1 for row in rows if row["paper_ready"]),
            "no_book_reason_counts": dict(Counter(row["no_book_reason"] for row in no_book_rows)),
            "malformed_or_unknown_reason_rows": sum(
                1 for row in positive_rows if row["no_book_reason"] not in BOOK_REASON_CODES
            ),
        },
        "positive_ev_rows": positive_rows,
        "current_positive_ev_rows": positive_rows,
        "expired_positive_ev_rows": expired_positive_rows,
        "historical_diagnostic_rows": [row for row in rows if row.get("diagnostic_only")],
        "finalized_or_settled_rows": finalized_rows,
        "no_executable_book_rows": no_book_rows,
        "acceptance": {
            "positive_ev_rows_explained_individually": all(
                bool(row.get("no_book_reason")) or row["executable_book"]
                for row in positive_rows
            ),
            "positive_ev_requires_executable_book": all(
                row["executable_book"] for row in positive_rows if row["paper_ready"]
            ),
            "stale_quote_never_executable": all(
                not row["executable_book"]
                for row in positive_rows
                if row["book_freshness_state"] == "STALE_ORDERBOOK"
            ),
        },
    }


def write_phase3ap_book_diagnostic_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ap"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> Phase3APDiagnosticArtifactSet:
    payload = build_phase3ap_book_diagnostic(
        session,
        output_dir=output_dir,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "book_diagnostic.json"
    markdown_path = output_dir / "book_diagnostic.md"
    _phase3ap_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3ap_book_markdown(payload), encoding="utf-8")
    return Phase3APDiagnosticArtifactSet(output_dir, json_path, markdown_path)


def build_phase3ap_refresh_positive_ev_books(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ap"),
    dry_run: bool = True,
    apply_readonly_refresh: bool = False,
    max_markets: int = 25,
    max_duration_seconds: int = 120,
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    book = build_phase3ap_book_diagnostic(
        session,
        output_dir=output_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    candidates = [
        row for row in book["positive_ev_rows"]
        if not row["executable_book"]
    ][:max_markets]
    writer = _phase3ap_db_writer_status(settings=resolved)
    blocked_by_writer = apply_readonly_refresh and not bool(writer.get("safe_to_start_write", True))
    refresh_artifact = None
    refresh_started = False
    refresh_completed = False
    status = "DRY_RUN"
    refresh_error = None
    if blocked_by_writer:
        status = "BLOCKED_BY_ACTIVE_WRITER"
    elif apply_readonly_refresh:
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
            status = "READONLY_REFRESH_COMPLETED"
            refresh_artifact = str(artifacts.json_path)
        except Exception as exc:  # noqa: BLE001 - operator report must capture refresh failure.
            status = "READONLY_REFRESH_FAILED"
            refresh_error = str(exc)
    metadata = _phase3ap_metadata(
        session,
        settings=resolved,
        output_dir=output_dir,
        command="kalshi-bot phase3ap-refresh-positive-ev-books",
        command_args={
            "output_dir": str(output_dir),
            "dry_run": dry_run,
            "apply_readonly_refresh": apply_readonly_refresh,
            "max_markets": max_markets,
            "max_duration_seconds": max_duration_seconds,
            "window_hours": window_hours,
            "limit": limit,
        },
    )
    return {
        "generated_at": metadata["generated_at"],
        "phase": "3AP",
        "phase_version": PHASE_3AP_UNBLOCK_VERSION,
        "mode": "PAPER_READ_ONLY_POSITIVE_EV_BOOK_REFRESH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "feature_writes": False,
        "forecast_writes": False,
        "opportunity_writes": False,
        "paper_trade_writes": False,
        "market_data_writes": bool(refresh_started and refresh_completed),
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "status": status,
        "dry_run": dry_run,
        "apply_readonly_refresh": apply_readonly_refresh,
        "active_writer": writer,
        "refresh_started": refresh_started,
        "refresh_completed": refresh_completed,
        "refresh_error": refresh_error,
        "refresh_artifact": refresh_artifact,
        "markets_needing_book_refresh": [
            row["market_ticker"] for row in candidates
        ],
        "markets_refreshed": [] if not refresh_completed else [row["market_ticker"] for row in candidates],
        "markets_blocked_by_writer": [row["market_ticker"] for row in candidates] if blocked_by_writer else [],
        "markets_not_found": [
            row["market_ticker"] for row in candidates
            if row["kalshi_url_status"] == "MARKET_NOT_IN_CATALOG"
        ],
        "markets_closed_or_paused": [
            row["market_ticker"] for row in candidates
            if row["no_book_reason"] in {"MARKET_NOT_OPEN", "MARKET_PAUSED"}
        ],
        "markets_still_empty_after_refresh": [],
        "markets_now_executable": [],
        "positive_ev_no_book_rows": candidates,
        "next_action": (
            "Wait for the active writer to finish, then rerun the dry-run diagnostic."
            if blocked_by_writer
            else (
                "Review the refresh artifact and rerun phase3ap-book-diagnostic."
                if refresh_completed
                else "Run again with --apply-readonly-refresh only after db-writer-monitor is clear."
            )
        ),
    }


def write_phase3ap_refresh_positive_ev_books_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ap"),
    dry_run: bool = True,
    apply_readonly_refresh: bool = False,
    max_markets: int = 25,
    max_duration_seconds: int = 120,
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> Phase3APDiagnosticArtifactSet:
    payload = build_phase3ap_refresh_positive_ev_books(
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
    json_path = output_dir / "refresh_positive_ev_books.json"
    markdown_path = output_dir / "refresh_positive_ev_books.md"
    _phase3ap_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3ap_refresh_markdown(payload), encoding="utf-8")
    return Phase3APDiagnosticArtifactSet(output_dir, json_path, markdown_path)


def build_phase3ap_settlement_check_diagnostic(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ap"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    funnel = build_paper_trade_funnel(
        session,
        window_hours=window_hours,
        replay_readonly=True,
        settings=resolved,
    )
    rows = list(funnel.get("rows") or [])[:limit]
    settlement_rows = [
        _settlement_diagnostic_row(session, row, settings=resolved)
        for row in rows
        if _looks_like_settlement_check_row(row)
    ]
    reason_counts = Counter(row["specific_reason_code"] for row in settlement_rows)
    metadata = _phase3ap_metadata(
        session,
        settings=resolved,
        output_dir=output_dir,
        command="kalshi-bot phase3ap-settlement-check-diagnostic",
        command_args={
            "output_dir": str(output_dir),
            "window_hours": window_hours,
            "limit": limit,
        },
    )
    return {
        "generated_at": metadata["generated_at"],
        "phase": "3AP",
        "phase_version": PHASE_3AP_UNBLOCK_VERSION,
        "mode": "PAPER_READ_ONLY_SETTLEMENT_CHECK_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "settlement_apply_ran": False,
        "allows_sibling_settlement": False,
        "allows_fuzzy_settlement": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "summary": {
            "rows_scanned": len(rows),
            "legacy_settlement_check_failed_rows": len(settlement_rows),
            "specific_reason_counts": dict(reason_counts),
            "open_market_entry_eligible_rows": sum(
                1 for row in settlement_rows if row["paper_entry_settlement_eligible"]
            ),
            "paper_entry_blocked_rows": sum(
                1 for row in settlement_rows if row["blocks_paper_entry"]
            ),
            "generic_settlement_check_failed_remaining": 0,
        },
        "specific_reason_counts": dict(reason_counts),
        "rows": settlement_rows,
        "acceptance": {
            "generic_reason_split": True,
            "open_known_terms_do_not_require_final_outcome": all(
                not row["blocks_paper_entry"]
                for row in settlement_rows
                if row["specific_reason_code"] == "MARKET_NOT_SETTLEABLE_YET"
                and row["settlement_terms_known"]
            ),
            "settled_resolution_logic_weakened": False,
        },
    }


def write_phase3ap_settlement_check_diagnostic_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ap"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> Phase3APDiagnosticArtifactSet:
    payload = build_phase3ap_settlement_check_diagnostic(
        session,
        output_dir=output_dir,
        settings=settings,
        window_hours=window_hours,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "settlement_check_diagnostic.json"
    markdown_path = output_dir / "settlement_check_diagnostic.md"
    _phase3ap_write_json(json_path, payload)
    markdown_path.write_text(_render_phase3ap_settlement_markdown(payload), encoding="utf-8")
    return Phase3APDiagnosticArtifactSet(output_dir, json_path, markdown_path)


def build_phase3ap_paper_ready_gate(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ap"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    rows = _phase3ap_gate_rows(
        session,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    reason_counts = Counter(row["primary_blocker"] for row in rows)
    all_positive_rows = [row for row in rows if _phase3ap_positive_ev(row)]
    current_positive_rows = [
        row for row in all_positive_rows if row.get("current_positive_ev_eligible")
    ]
    expired_positive_rows = [
        row for row in all_positive_rows if row.get("window_status") == EXPIRED_WINDOW_EXCLUDED
    ]
    finalized_rows = [
        row for row in all_positive_rows if row.get("window_status") == MARKET_CLOSED_OR_SETTLED
    ]
    first_hard_blocker = _phase3ap_first_hard_blocker(
        current_positive_rows=current_positive_rows,
        reason_counts=reason_counts,
    )
    metadata = _phase3ap_metadata(
        session,
        settings=resolved,
        output_dir=output_dir,
        command="kalshi-bot phase3ap-paper-ready-unblock-report",
        command_args={
            "output_dir": str(output_dir),
            "window_hours": window_hours,
            "limit": limit,
        },
    )
    return {
        "generated_at": metadata["generated_at"],
        "phase": "3AP",
        "phase_version": PHASE_3AP_UNBLOCK_VERSION,
        "mode": "PAPER_READ_ONLY_CANONICAL_PAPER_READY_GATE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "thresholds": _phase3ap_thresholds(resolved),
        "summary": {
            "rows_scanned": len(rows),
            "paper_ready_rows": sum(1 for row in rows if row["paper_ready"]),
            "positive_ev_rows": len(current_positive_rows),
            "current_positive_ev_rows": len(current_positive_rows),
            "all_positive_ev_rows_including_diagnostics": len(all_positive_rows),
            "expired_positive_ev_rows": len(expired_positive_rows),
            "expired_excluded_rows": len(expired_positive_rows),
            "historical_diagnostic_rows": sum(1 for row in rows if row.get("diagnostic_only")),
            "finalized_or_settled_rows": len(finalized_rows),
            "stale_catalog_rows": sum(
                1 for row in current_positive_rows if row.get("primary_blocker") == STALE_CATALOG
            ),
            "stale_quote_rows": sum(
                1 for row in current_positive_rows if row.get("primary_blocker") == "STALE_QUOTE"
            ),
            "positive_ev_no_executable_book_rows": sum(
                1
                for row in current_positive_rows
                if not row["executable_book"]
            ),
            "first_hard_blocker": first_hard_blocker,
            "reason_counts": dict(reason_counts),
            "all_paper_ready_have_verified_kalshi_links": all(
                row["kalshi_url_verified"] for row in rows if row["paper_ready"]
            ),
            "all_paper_ready_have_executable_books": all(
                row["executable_book"] for row in rows if row["paper_ready"]
            ),
        },
        "reason_hierarchy": list(PAPER_READY_REASON_HIERARCHY),
        "rows": rows,
    }


def write_phase3ap_paper_ready_unblock_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ap"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    window_hours: int = 168,
    limit: int = DEFAULT_PHASE3AP_SCAN_LIMIT,
) -> Phase3APUnblockArtifactSet:
    resolved = settings or get_settings()
    output_dir.mkdir(parents=True, exist_ok=True)
    book = build_phase3ap_book_diagnostic(
        session,
        output_dir=output_dir,
        settings=resolved,
        window_hours=window_hours,
        limit=limit,
    )
    settlement = build_phase3ap_settlement_check_diagnostic(
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
    phase3an = _phase3ap_phase3an_context(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        window_hours=window_hours,
    )
    book_json_path = output_dir / "book_diagnostic.json"
    book_markdown_path = output_dir / "book_diagnostic.md"
    settlement_json_path = output_dir / "settlement_check_diagnostic.json"
    settlement_markdown_path = output_dir / "settlement_check_diagnostic.md"
    gate_path = output_dir / "paper_ready_gate.json"
    positive_ev_csv = output_dir / "positive_ev_rows.csv"
    no_book_csv = output_dir / "no_executable_book_rows.csv"
    settlement_csv = output_dir / "settlement_blocked_rows.csv"
    executive_summary = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions = output_dir / "NEXT_ACTIONS.md"
    manifest = output_dir / "MANIFEST.sha256"

    _phase3ap_write_json(book_json_path, book)
    book_markdown_path.write_text(_render_phase3ap_book_markdown(book), encoding="utf-8")
    _phase3ap_write_json(settlement_json_path, settlement)
    settlement_markdown_path.write_text(
        _render_phase3ap_settlement_markdown(settlement),
        encoding="utf-8",
    )
    _phase3ap_write_json(gate_path, gate)
    _write_csv(positive_ev_csv, book["positive_ev_rows"])
    _write_csv(no_book_csv, book["no_executable_book_rows"])
    _write_csv(
        settlement_csv,
        [row for row in settlement["rows"] if row["blocks_paper_entry"]],
    )
    executive_summary.write_text(
        _render_phase3ap_executive_summary(
            book=book,
            settlement=settlement,
            gate=gate,
            phase3an=phase3an,
        ),
        encoding="utf-8",
    )
    next_actions.write_text(
        _render_phase3ap_next_actions(book=book, settlement=settlement, gate=gate),
        encoding="utf-8",
    )
    _write_manifest(
        manifest,
        [
            executive_summary,
            next_actions,
            book_json_path,
            book_markdown_path,
            settlement_json_path,
            settlement_markdown_path,
            gate_path,
            positive_ev_csv,
            no_book_csv,
            settlement_csv,
        ],
    )
    return Phase3APUnblockArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary,
        next_actions_path=next_actions,
        book_json_path=book_json_path,
        book_markdown_path=book_markdown_path,
        settlement_json_path=settlement_json_path,
        settlement_markdown_path=settlement_markdown_path,
        paper_ready_gate_path=gate_path,
        positive_ev_csv_path=positive_ev_csv,
        no_executable_book_csv_path=no_book_csv,
        settlement_blocked_csv_path=settlement_csv,
        manifest_path=manifest,
    )


def _phase3ap_gate_rows(
    session: Session,
    *,
    settings: Settings,
    window_hours: int,
    limit: int,
) -> list[dict[str, Any]]:
    now = utc_now()
    cutoff = now - timedelta(hours=window_hours)
    rankings = list(
        session.scalars(
            select(MarketRanking)
            .where(MarketRanking.ranked_at >= cutoff)
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.opportunity_score))
            .limit(limit)
        )
    )
    tickers = sorted({row.ticker for row in rankings})
    forecasts = _latest_by_ticker_model(session, Forecast, tickers, "forecasted_at")
    snapshots = _latest_by_ticker(session, MarketSnapshot, tickers, "captured_at")
    sizing = _latest_by_ticker(session, PositionSizingDecisionLog, tickers, "decision_timestamp")
    risk = _latest_by_ticker(session, AdvancedRiskDecisionLog, tickers, "decision_timestamp")
    paper_orders = _paper_order_keys(session, tickers)
    return [
        _phase3ap_gate_row(
            session,
            ranking,
            forecast=forecasts.get((ranking.ticker, ranking.forecast_model)),
            snapshot=snapshots.get(ranking.ticker),
            sizing=sizing.get(ranking.ticker),
            risk=risk.get(ranking.ticker),
            paper_orders=paper_orders,
            now=now,
            settings=settings,
        )
        for ranking in rankings
    ]


def _phase3ap_gate_row(
    session: Session,
    ranking: MarketRanking,
    *,
    forecast: Forecast | None,
    snapshot: MarketSnapshot | None,
    sizing: PositionSizingDecisionLog | None,
    risk: AdvancedRiskDecisionLog | None,
    paper_orders: set[tuple[str, str, int | None]],
    now: Any,
    settings: Settings,
) -> dict[str, Any]:
    market = session.get(Market, ranking.ticker)
    identity = verify_market_identity(session, ranking=ranking, market=market, settings=settings)
    probability = to_decimal(forecast.yes_probability if forecast else ranking.forecast_probability)
    side = str(ranking.best_side or "")
    side_probability = None
    if probability is not None:
        side_probability = Decimal("1") - probability if side == BUY_NO else probability
    price = to_decimal(ranking.best_price)
    raw_ev = side_probability - price if side_probability is not None and price is not None else None
    spread = to_decimal(ranking.spread)
    executable_ev = (
        raw_ev - (spread or Decimal("0")) - RAW_EV_COST_BUFFER
        if raw_ev is not None
        else None
    )
    quote_age = _age_minutes(snapshot.captured_at, now) if snapshot is not None else None
    window = current_market_window_status(
        market,
        settings=settings,
        ranking=ranking,
        now=now,
    )
    book = _phase3ap_book_probe(
        ranking=ranking,
        market=market,
        identity=identity.as_dict(),
        snapshot=snapshot,
        side=side,
        quote_age=quote_age,
        settings=settings,
        window=window,
    )
    settlement = _settlement_entry_check(
        session,
        ticker=ranking.ticker,
        market=market,
        identity=identity.as_dict(),
    )
    forecast_id = _forecast_id_from_ranking(ranking)
    duplicate = (ranking.ticker, ranking.forecast_model, forecast_id) in paper_orders
    phase3s_pass = (to_decimal(ranking.opportunity_score) or Decimal("0")) >= settings.opportunity_min_score
    phase3m_contracts = int(getattr(sizing, "proposed_contracts", 0) or 0)
    phase3m_nonzero = phase3m_contracts > 0
    phase3n_action = str(getattr(risk, "action", "") or "").upper()
    phase3n_approved = phase3n_action in {"ALLOW", "APPROVE", "PROCEED"}
    primary, secondary = _paper_ready_blockers(
        identity=identity.as_dict(),
        market=market,
        window=window,
        quote_age=quote_age,
        book=book,
        raw_ev=raw_ev,
        executable_ev=executable_ev,
        confidence=to_decimal(ranking.model_confidence_score),
        settlement=settlement,
        phase3s_pass=phase3s_pass,
        phase3m_nonzero=phase3m_nonzero,
        phase3n_approved=phase3n_approved,
        duplicate=duplicate,
    )
    paper_ready = primary == "PAPER_READY"
    base = {
        "ticker": ranking.ticker,
        "market_ticker": ranking.ticker,
        "forecast_model": ranking.forecast_model,
        "forecast_probability": decimal_to_str(probability),
        "model_probability": decimal_to_str(probability),
        "market_price": decimal_to_str(price),
        "raw_ev": decimal_to_str(raw_ev),
        "executable_ev": decimal_to_str(executable_ev),
        "best_side": side,
        "best_price": decimal_to_str(price),
        "best_yes_bid": book["best_yes_bid"],
        "best_yes_ask": book["best_yes_ask"],
        "best_no_bid": book["best_no_bid"],
        "best_no_ask": book["best_no_ask"],
        "derived_executable_buy_price": book["derived_executable_buy_price"],
        "spread": decimal_to_str(spread),
        "visible_depth": book["visible_depth"],
        "depth_at_configured_limit": book["depth_at_configured_limit"],
        "quote_timestamp": snapshot.captured_at.isoformat() if snapshot else None,
        "quote_age_minutes": decimal_to_str(quote_age),
        "book_source": book["book_source"],
        "book_freshness_state": book["book_freshness_state"],
        "liquidity_score": ranking.liquidity_score,
        "executable_book": book["executable_book"],
        "no_book_reason": book["no_book_reason"],
        "book_reason": book["book_reason"],
        "window_status": window["window_status"],
        "current_window_status": window["current_window_status"],
        "window_status_reason": window["window_status_reason"],
        "current_window_eligible": window["current_window_eligible"],
        "current_positive_ev_eligible": window["current_positive_ev_eligible"],
        "diagnostic_only": window["diagnostic_only"],
        "expired_window_excluded": window["expired_window_excluded"],
        "market_close_time": window.get("market_close_time"),
        "expected_expiration_time": window.get("expected_expiration_time"),
        "expiration_time": window.get("expiration_time"),
        "market_settlement_ts": window.get("settlement_ts"),
        "final_entry_cutoff_time": window.get("final_entry_cutoff_time"),
        "window_minutes_to_close": window.get("minutes_to_close"),
        "paper_ready": paper_ready,
        "paper_ready_blocker": None if paper_ready else primary,
        "primary_blocker": "PAPER_READY" if paper_ready else primary,
        "secondary_blockers": secondary,
        "settlement_specific_reason": settlement["specific_reason_code"],
        "settlement_terms_known": settlement["settlement_terms_known"],
        "paper_entry_settlement_eligible": settlement["paper_entry_settlement_eligible"],
        "market_lifecycle_status": identity.market_lifecycle_status,
        "catalog_last_seen_at": identity.catalog_last_seen_at,
        "source_lineage": identity.source_lineage,
        "phase3s_proceed": phase3s_pass,
        "phase3m_nonzero_size": phase3m_nonzero,
        "phase3m_proposed_contracts": phase3m_contracts,
        "phase3n_approved": phase3n_approved,
        "phase3n_action": phase3n_action or None,
        "duplicate_idempotency_key": duplicate,
        "what_would_make_paper_ready": _what_would_make_paper_ready(primary, book, settlement),
        "ranking_id": ranking.id,
        "forecast_id": forecast_id,
        "ranked_at": ranking.ranked_at.isoformat() if ranking.ranked_at else None,
        "forecasted_at": forecast.forecasted_at.isoformat() if forecast else None,
    }
    base.update(market_identity_fields(identity))
    base["market_identity"] = identity.as_dict()
    return base


def _phase3ap_book_probe(
    *,
    ranking: MarketRanking,
    market: Market | None,
    identity: dict[str, Any],
    snapshot: MarketSnapshot | None,
    side: str,
    quote_age: Decimal | None,
    settings: Settings,
    window: dict[str, Any],
) -> dict[str, Any]:
    raw_orderbook = decode_json(snapshot.raw_orderbook_json if snapshot else None)
    prices = parse_orderbook(raw_orderbook)
    book = (
        usable_bid_ask_book(
            raw_orderbook,
            side=side,
            liquidity_score=ranking.liquidity_score,
            min_liquidity_score=MIN_EXECUTABLE_LIQUIDITY_SCORE,
            max_spread=settings.opportunity_max_spread,
        )
        if side in {BUY_YES, BUY_NO}
        else None
    )
    freshness_state = "FRESH"
    reason = None
    if not window.get("current_window_eligible"):
        reason = str(window.get("window_status") or MARKET_NOT_OPEN)
        freshness_state = "EXPIRED_WINDOW" if reason == EXPIRED_WINDOW_EXCLUDED else "MARKET_NOT_OPEN"
    elif not identity.get("kalshi_url_verified"):
        status = str(identity.get("kalshi_url_status") or identity.get("url_verification_status"))
        if status in {SYNTHETIC_ONLY, COMPOSITE_LOCAL_ONLY}:
            reason = "SYNTHETIC_OR_COMPOSITE_ONLY"
        elif status == BUILT_FROM_EXACT_CATALOG:
            reason = BUILT_FROM_EXACT_CATALOG
        else:
            reason = "UNVERIFIED_KALSHI_LINK"
    elif _market_paused(market):
        reason = "MARKET_PAUSED"
    elif not _market_open_for_entry(market):
        reason = "MARKET_NOT_OPEN"
    elif _market_close_too_near(ranking, settings):
        reason = "MARKET_CLOSE_TOO_NEAR"
    elif snapshot is None or snapshot.raw_orderbook_json is None:
        reason = "NO_ORDERBOOK_SNAPSHOT"
        freshness_state = "NO_ORDERBOOK_SNAPSHOT"
    elif quote_age is None or quote_age > QUOTE_STALE_AFTER_MINUTES:
        reason = "STALE_ORDERBOOK"
        freshness_state = "STALE_ORDERBOOK"
    elif _empty_orderbook(raw_orderbook):
        reason = "EMPTY_ORDERBOOK"
    elif book is None:
        reason = "UNKNOWN_REQUIRES_INVESTIGATION"
    elif book.spread is not None and book.spread < 0:
        reason = "CROSSED_OR_INCONSISTENT_BOOK"
    elif not book.has_visible_bid_ask:
        reason = "ZERO_VISIBLE_DEPTH"
    elif not book.has_executable_depth:
        reason = "INSUFFICIENT_DEPTH"
    elif book.spread is not None and book.spread > settings.opportunity_max_spread:
        reason = "WIDE_SPREAD"
    elif book.liquidity_score is None or book.liquidity_score <= 0:
        reason = "ZERO_VISIBLE_DEPTH"
    elif book.liquidity_score < MIN_EXECUTABLE_LIQUIDITY_SCORE:
        reason = "INSUFFICIENT_DEPTH"
    executable = reason is None and bool(book and book.usable)
    if executable:
        reason = None
    ask_price = book.ask_price if book is not None else None
    bid_depth = book.bid_depth if book is not None else None
    ask_depth = book.ask_depth if book is not None else None
    return {
        "best_yes_bid": decimal_to_str(prices.best_yes_bid),
        "best_yes_ask": decimal_to_str(prices.best_yes_ask),
        "best_no_bid": decimal_to_str(prices.best_no_bid),
        "best_no_ask": decimal_to_str(prices.best_no_ask),
        "derived_executable_buy_price": decimal_to_str(ask_price),
        "visible_depth": decimal_to_str((bid_depth or Decimal("0")) + (ask_depth or Decimal("0"))),
        "depth_at_configured_limit": decimal_to_str(ask_depth),
        "book_source": "market_snapshots.raw_orderbook_json" if snapshot else "missing_snapshot",
        "book_freshness_state": freshness_state,
        "executable_book": executable,
        "no_book_reason": reason,
        "book_reason": "Executable book passes configured gates." if executable else (book.reason if book else reason),
    }


def _settlement_diagnostic_row(
    session: Session,
    row: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "")
    market = session.get(Market, ticker) if ticker else None
    ranking = session.get(MarketRanking, row.get("ranking_id")) if row.get("ranking_id") else None
    identity = verify_market_identity(session, ticker=ticker, ranking=ranking, market=market, settings=settings)
    check = _settlement_entry_check(
        session,
        ticker=ticker,
        market=market,
        identity=identity.as_dict(),
    )
    return {
        "ticker": ticker,
        "source_reason_code": row.get("reason_code"),
        "specific_reason_code": check["specific_reason_code"],
        "blocks_paper_entry": check["blocks_paper_entry"],
        "paper_entry_settlement_eligible": check["paper_entry_settlement_eligible"],
        "settlement_terms_known": check["settlement_terms_known"],
        "market_status": getattr(market, "status", None),
        "market_close_time": market.close_time.isoformat() if market and market.close_time else None,
        "market_settlement_ts": market.settlement_ts.isoformat() if market and market.settlement_ts else None,
        "rules_primary_present": bool(getattr(market, "rules_primary", None)),
        "rules_secondary_present": bool(getattr(market, "rules_secondary", None)),
        "market_result": getattr(market, "result", None),
        "settlement_found": check["settlement_found"],
        "settlement_result": check["settlement_result"],
        "reason": check["reason"],
        "market_identity": identity.as_dict(),
    }


def _settlement_entry_check(
    session: Session,
    *,
    ticker: str,
    market: Market | None,
    identity: dict[str, Any],
) -> dict[str, Any]:
    settlement = session.get(Settlement, ticker) if ticker else None
    status = str(identity.get("kalshi_url_status") or identity.get("url_verification_status") or "")
    if status == SYNTHETIC_ONLY:
        code = "SYNTHETIC_MARKET_NO_SETTLEMENT_RULE"
        blocks = True
    elif status == COMPOSITE_LOCAL_ONLY:
        code = "COMPOSITE_MARKET_REQUIRES_RESOLVER"
        blocks = True
    elif status == GENERAL_SOURCE_NOT_SAFE:
        code = "GENERAL_SOURCE_NOT_FORECAST_SAFE"
        blocks = True
    elif status == PLACEHOLDER_BLOCKED:
        code = "SPORTS_PLACEHOLDER_BLOCKED"
        blocks = True
    elif status == PARTIAL_PROVENANCE_BLOCKED:
        code = "PARTIAL_PROVENANCE_BLOCKED"
        blocks = True
    elif market is None:
        code = "MARKET_SETTLEMENT_STATUS_UNKNOWN"
        blocks = True
    elif not (market.rules_primary or market.rules_secondary):
        code = "SETTLEMENT_RULE_MISSING"
        blocks = True
    elif market.close_time is None:
        code = "MARKET_CLOSE_UNKNOWN"
        blocks = True
    elif not market.status:
        code = "MARKET_SETTLEMENT_STATUS_UNKNOWN"
        blocks = True
    elif _market_open_for_entry(market):
        code = "MARKET_NOT_SETTLEABLE_YET"
        blocks = False
    elif settlement is not None and settlement.result is None:
        code = "MARKET_ALREADY_SETTLED_BUT_OUTCOME_MISSING"
        blocks = True
    elif settlement is None and market.result:
        code = "SETTLEMENT_SOURCE_MISSING"
        blocks = True
    elif settlement is None:
        code = "MARKET_NOT_SETTLEABLE_YET"
        blocks = True
    else:
        code = "MARKET_NOT_SETTLEABLE_YET"
        blocks = False
    terms_known = bool(market and (market.rules_primary or market.rules_secondary) and market.close_time)
    return {
        "specific_reason_code": code,
        "blocks_paper_entry": blocks,
        "paper_entry_settlement_eligible": terms_known and not blocks,
        "settlement_terms_known": terms_known,
        "settlement_found": settlement is not None,
        "settlement_result": settlement.result if settlement is not None else None,
        "reason": _settlement_reason_text(code, blocks=blocks, terms_known=terms_known),
    }


def _paper_ready_blockers(
    *,
    identity: dict[str, Any],
    market: Market | None,
    window: dict[str, Any],
    quote_age: Decimal | None,
    book: dict[str, Any],
    raw_ev: Decimal | None,
    executable_ev: Decimal | None,
    confidence: Decimal | None,
    settlement: dict[str, Any],
    phase3s_pass: bool,
    phase3m_nonzero: bool,
    phase3n_approved: bool,
    duplicate: bool,
) -> tuple[str, list[str]]:
    blockers: list[str] = []
    if not window.get("current_window_eligible"):
        blockers.append(str(window.get("window_status") or MARKET_NOT_OPEN))
    if not identity.get("kalshi_url_verified"):
        identity_status = identity.get("kalshi_url_status")
        if identity_status in {CATALOG_STALE, STALE_CATALOG}:
            blockers.append(STALE_CATALOG)
        elif identity_status == GENERAL_SOURCE_NOT_SAFE:
            blockers.append("SOURCE_NOT_FORECAST_SAFE")
        elif identity_status == MALFORMED_URL:
            blockers.append("MALFORMED_URL")
        elif identity_status == BUILT_FROM_EXACT_CATALOG:
            blockers.append(BUILT_FROM_EXACT_CATALOG)
        elif identity_status in {MARKET_NOT_IN_CATALOG, CATALOG_MATCH_MISSING}:
            blockers.append("CATALOG_MATCH_MISSING")
        else:
            blockers.append("UNVERIFIED_KALSHI_LINK")
    if not _market_open_for_entry(market):
        blockers.append("MARKET_NOT_OPEN")
    if quote_age is None or quote_age > QUOTE_STALE_AFTER_MINUTES:
        blockers.append("STALE_QUOTE")
    if not book["executable_book"]:
        no_book_reason = book.get("no_book_reason")
        if no_book_reason in {"NO_ORDERBOOK_SNAPSHOT", "EMPTY_ORDERBOOK", "ZERO_VISIBLE_DEPTH"}:
            blockers.append(no_book_reason)
        elif no_book_reason == "WIDE_SPREAD":
            blockers.append("SPREAD_TOO_WIDE")
        elif no_book_reason in {"INSUFFICIENT_DEPTH", "ZERO_VISIBLE_DEPTH"}:
            blockers.append("LIQUIDITY_TOO_LOW")
        else:
            blockers.append("NO_ORDERBOOK_SNAPSHOT")
    if book["no_book_reason"] == "WIDE_SPREAD":
        blockers.append("SPREAD_TOO_WIDE")
    if book["no_book_reason"] in {"ZERO_VISIBLE_DEPTH", "INSUFFICIENT_DEPTH"}:
        blockers.append("LIQUIDITY_TOO_LOW")
    if raw_ev is None or raw_ev <= 0:
        blockers.append("NO_POSITIVE_EXECUTABLE_EV")
    if executable_ev is None or executable_ev <= 0:
        blockers.append("NO_POSITIVE_EXECUTABLE_EV")
    if confidence is not None and confidence < Decimal("40"):
        blockers.append("CONFIDENCE_BELOW_THRESHOLD")
    if settlement["blocks_paper_entry"]:
        blockers.append("SETTLEMENT_TERMS_UNKNOWN")
    if not phase3s_pass:
        blockers.append("PHASE_3S_SKIP")
    if not phase3m_nonzero:
        blockers.append("PHASE_3M_ZERO_SIZE")
    if not phase3n_approved:
        blockers.append("PHASE_3N_RISK_BLOCK")
    if duplicate:
        blockers.append("DUPLICATE_IDEMPOTENCY_KEY")
    if not blockers:
        return "PAPER_READY", []
    ordered = [reason for reason in PAPER_READY_REASON_HIERARCHY if reason in blockers]
    if not ordered:
        return "UNKNOWN_REQUIRES_INVESTIGATION", blockers
    return ordered[0], [reason for reason in ordered[1:]]


def _latest_by_ticker(
    session: Session,
    model: Any,
    tickers: list[str],
    time_attr: str,
) -> dict[str, Any]:
    if not tickers:
        return {}
    column = getattr(model, time_attr)
    rows = list(
        session.scalars(
            select(model)
            .where(model.ticker.in_(tickers))
            .order_by(model.ticker, desc(column), desc(model.id) if hasattr(model, "id") else desc(column))
        )
    )
    latest: dict[str, Any] = {}
    for row in rows:
        latest.setdefault(row.ticker, row)
    return latest


def _latest_by_ticker_model(
    session: Session,
    model: Any,
    tickers: list[str],
    time_attr: str,
) -> dict[tuple[str, str], Any]:
    if not tickers:
        return {}
    column = getattr(model, time_attr)
    rows = list(
        session.scalars(
            select(model)
            .where(model.ticker.in_(tickers))
            .order_by(model.ticker, model.model_name, desc(column), desc(model.id))
        )
    )
    latest: dict[tuple[str, str], Any] = {}
    for row in rows:
        latest.setdefault((row.ticker, row.model_name), row)
    return latest


def _paper_order_keys(session: Session, tickers: list[str]) -> set[tuple[str, str, int | None]]:
    if not tickers:
        return set()
    rows = session.scalars(select(PaperOrder).where(PaperOrder.ticker.in_(tickers)))
    return {(row.ticker, row.model_name, row.forecast_id) for row in rows}


def _phase3ap_metadata(
    session: Session,
    *,
    settings: Settings,
    output_dir: Path,
    command: str,
    command_args: dict[str, Any],
) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    redacted_db_url = redact_database_url(db_url)
    db_location = describe_db_location(db_url)
    args = {"command": command, **command_args}
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AP",
        "phase_version": PHASE_3AP_UNBLOCK_VERSION,
        "repository_root": str(Path.cwd().resolve()),
        "git_branch": _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": _git_dirty_status(),
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(__file__).resolve()),
        "resolved_database_url": redacted_db_url,
        "database_fingerprint": _phase3ap_database_fingerprint(
            redacted_db_url=redacted_db_url,
            location=db_location,
        ),
        "database_location": db_location,
        "migration_revision": _phase3ap_migration_revision(session),
        "timezone": getattr(settings, "timezone", None) or "UTC",
        "command_arguments": args,
        "data_watermark": _phase3an_data_watermark(session),
        "safety_flags": _phase3an_safety_flags(),
        "active_db_writer_status": "NOT_CHECKED_FOR_READ_ONLY_DIAGNOSTIC",
        "ui_database_identity": {"database_fingerprint": redacted_db_url},
        "cli_database_identity": {"database_fingerprint": redacted_db_url},
        "worker_database_identity": {"database_fingerprint": redacted_db_url},
    }


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "UNKNOWN"


def _git_dirty_status() -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    if result.returncode != 0:
        return "UNKNOWN"
    return "dirty" if result.stdout.strip() else "clean"


def _phase3ap_database_fingerprint(*, redacted_db_url: str, location: str) -> str:
    payload = json.dumps(
        {"database_url": redacted_db_url, "location": location},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _phase3ap_migration_revision(session: Session) -> str | None:
    try:
        return session.execute(text("select version_num from alembic_version limit 1")).scalar()
    except Exception:  # noqa: BLE001 - metadata must be best-effort and bounded.
        return None


def _phase3ap_thresholds(settings: Settings) -> dict[str, str]:
    return {
        "opportunity_min_edge": str(settings.opportunity_min_edge),
        "opportunity_min_score": str(settings.opportunity_min_score),
        "opportunity_max_spread": str(settings.opportunity_max_spread),
        "opportunity_min_liquidity": str(settings.opportunity_min_liquidity),
        "opportunity_min_time_to_close_minutes": str(settings.opportunity_min_time_to_close_minutes),
        "quote_stale_after_minutes": str(QUOTE_STALE_AFTER_MINUTES),
        "min_executable_liquidity_score": str(MIN_EXECUTABLE_LIQUIDITY_SCORE),
        "thresholds_lowered": "False",
    }


def _phase3ap_db_writer_status(*, settings: Settings) -> dict[str, Any]:
    from kalshi_predictor.data.locks import db_writer_monitor

    try:
        return db_writer_monitor(settings=settings)
    except Exception as exc:  # noqa: BLE001 - refresh command must terminate.
        return {
            "status": "UNKNOWN_REQUIRES_INVESTIGATION",
            "safe_to_start_write": False,
            "error": str(exc),
        }


def _phase3ap_phase3an_context(
    session: Session,
    *,
    output_dir: Path,
    reports_dir: Path,
    settings: Settings,
    window_hours: int,
) -> dict[str, Any]:
    return {
        "paper_funnel": build_phase3an_paper_funnel_explain(
            session,
            window_hours=window_hours,
            output_dir=output_dir,
            settings=settings,
        ),
        "settlement": build_phase3an_settlement_health_confirm(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
        ),
        "general_sources": build_phase3an_general_sources_status(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            sources_dir=reports_dir / "phase3bb_r2_sources",
            evidence_dir=Path("data/general_source_evidence"),
            settings=settings,
        ),
        "sports": build_phase3an_sports_blocker_report(
            output_dir=output_dir,
            reports_dir=reports_dir,
        ),
        "economic_news": build_phase3an_economic_news_watch(
            session,
            output_dir=output_dir,
            settings=settings,
        ),
    }


def _age_minutes(value: Any, now: Any) -> Decimal | None:
    dt = value if hasattr(value, "astimezone") else parse_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return Decimal(str(max(0, (now - dt.astimezone(now.tzinfo)).total_seconds()))) / Decimal("60")


def _phase3ap_positive_ev(row: dict[str, Any]) -> bool:
    return (to_decimal(row.get("raw_ev")) or Decimal("0")) > 0


def _phase3ap_first_hard_blocker(
    *,
    current_positive_rows: list[dict[str, Any]],
    reason_counts: Counter,
) -> str:
    if not current_positive_rows:
        return "NO_CURRENT_POSITIVE_EV"
    blocked_counts = Counter(
        row.get("primary_blocker")
        for row in current_positive_rows
        if row.get("primary_blocker") != "PAPER_READY"
    )
    if blocked_counts:
        return str(blocked_counts.most_common(1)[0][0])
    if reason_counts:
        return str(reason_counts.most_common(1)[0][0])
    return "UNKNOWN_REQUIRES_INVESTIGATION"


def _forecast_id_from_ranking(ranking: MarketRanking) -> int | None:
    raw = decode_json(ranking.raw_json)
    value = raw.get("forecast_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _empty_orderbook(raw_orderbook: dict[str, Any]) -> bool:
    if not raw_orderbook:
        return True
    container = raw_orderbook.get("orderbook_fp") or raw_orderbook.get("orderbook") or raw_orderbook
    yes = container.get("yes_dollars", container.get("yes")) if isinstance(container, dict) else None
    no = container.get("no_dollars", container.get("no")) if isinstance(container, dict) else None
    return not yes and not no


def _market_open_for_entry(market: Market | None) -> bool:
    if market is None:
        return False
    status = str(market.status or "").lower()
    if any(token in status for token in ("closed", "settled", "final", "expired", "paused", "halt")):
        return False
    now = utc_now()
    close_time = _aware_utc(market.close_time)
    if close_time is not None and close_time <= now:
        return False
    return status in {"open", "active", "trading"} or not status


def _aware_utc(value: Any) -> Any:
    dt = value if hasattr(value, "astimezone") else parse_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _market_paused(market: Market | None) -> bool:
    status = str(getattr(market, "status", "") or "").lower()
    return "paused" in status or "halt" in status or "suspend" in status


def _market_close_too_near(ranking: MarketRanking, settings: Settings) -> bool:
    value = to_decimal(ranking.time_to_close_minutes)
    return value is not None and value < settings.opportunity_min_time_to_close_minutes


def _looks_like_settlement_check_row(row: dict[str, Any]) -> bool:
    reason = str(row.get("reason_code") or "")
    return reason in {"SETTLEMENT_CHECK_FAILED", "EXPIRED_CRYPTO_WINDOW", "WAITING_FOR_SETTLEMENT"}


def _settlement_reason_text(code: str, *, blocks: bool, terms_known: bool) -> str:
    if code == "MARKET_NOT_SETTLEABLE_YET" and terms_known and not blocks:
        return "Market is open with known settlement terms; final outcome is not required for paper entry."
    if code == "SETTLEMENT_RULE_MISSING":
        return "Market lacks local settlement rules/terms needed for safe paper entry."
    if code == "MARKET_CLOSE_UNKNOWN":
        return "Market close time is unknown, so paper entry cannot bound settlement timing."
    if code == "GENERAL_SOURCE_NOT_FORECAST_SAFE":
        return "General-source evidence gates have not marked this row forecast-safe."
    if code == "SPORTS_PLACEHOLDER_BLOCKED":
        return "Sports placeholder provenance cannot support paper entry."
    if code == "PARTIAL_PROVENANCE_BLOCKED":
        return "Partial provenance cannot support paper entry."
    return code.replace("_", " ").title()


def _what_would_make_paper_ready(
    primary: str,
    book: dict[str, Any],
    settlement: dict[str, Any],
) -> list[str]:
    if primary == "PAPER_READY":
        return ["All gates passed; review paper-only execution controls before any order flow."]
    if primary == EXPIRED_WINDOW_EXCLUDED:
        return ["Expired window excluded from current positive-EV and paper-ready gates."]
    if primary == MARKET_CLOSED_OR_SETTLED:
        return ["Closed, finalized, or settled market kept diagnostic-only."]
    if primary == MARKET_CLOSE_TOO_NEAR:
        return ["Wait for a future market outside the configured final paper-entry cutoff."]
    if primary in {"NO_ORDERBOOK_SNAPSHOT", "EMPTY_ORDERBOOK", "ZERO_VISIBLE_DEPTH"}:
        return [book.get("book_reason") or "Wait for a fresh visible executable book."]
    if primary == "SETTLEMENT_TERMS_UNKNOWN":
        return [settlement.get("reason") or "Repair settlement terms/source evidence."]
    mapping = {
        "UNVERIFIED_KALSHI_LINK": "Repair exact Kalshi market identity and canonical URL.",
        "BUILT_FROM_EXACT_CATALOG": "Run Phase 3AR URL repair to persist an official URL before paper entry.",
        "MALFORMED_URL": "Run Phase 3AR URL repair for exact catalog matches.",
        "CATALOG_MATCH_MISSING": "Repair exact catalog market identity before any book refresh.",
        "MARKET_NOT_OPEN": "Wait for an open/tradeable market lifecycle.",
        "STALE_CATALOG": "Refresh exact current market catalog metadata before URL verification.",
        "STALE_QUOTE": "Refresh the exact market snapshot/orderbook.",
        "SPREAD_TOO_WIDE": "Wait for spread below the configured threshold.",
        "LIQUIDITY_TOO_LOW": "Wait for visible depth/liquidity above configured thresholds.",
        "NO_POSITIVE_EXECUTABLE_EV": "Wait for executable EV to remain positive after spread/costs.",
        "CONFIDENCE_BELOW_THRESHOLD": "Wait for model confidence to pass the configured threshold.",
        "SOURCE_NOT_FORECAST_SAFE": "Complete exact source-evidence gates before promotion.",
        "PHASE_3S_SKIP": "Wait for Phase 3S to say proceed.",
        "PHASE_3M_ZERO_SIZE": "Run/repair Phase 3M sizing only after market gates pass.",
        "PHASE_3N_RISK_BLOCK": "Run/repair Phase 3N paper risk approval only after sizing passes.",
        "DUPLICATE_IDEMPOTENCY_KEY": "Do not duplicate an existing paper intent for the same forecast.",
    }
    return [mapping.get(primary, "Investigate the unknown paper-ready blocker.")]


def _render_phase3ap_book_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AP Book Diagnostic",
        "",
        f"- Generated at: {payload['generated_at']}",
        "- Mode: PAPER / READ ONLY",
        f"- Positive EV rows: {summary['positive_ev_rows']}",
        f"- Positive EV with no executable book: {summary['positive_ev_no_executable_book_rows']}",
        "",
        "| Ticker | Kalshi status | Raw EV | Exec EV | Book | Reason | Quote age | Spread | Depth | Next |",
        "|---|---|---:|---:|---|---|---:|---:|---:|---|",
    ]
    for row in payload["positive_ev_rows"][:50]:
        lines.append(
            f"| {row['market_ticker']} | {row['kalshi_url_status']} | {row['raw_ev']} | "
            f"{row['executable_ev']} | {row['executable_book']} | {row['no_book_reason'] or 'EXECUTABLE'} | "
            f"{row['quote_age_minutes'] or 'n/a'} | {row['spread'] or 'n/a'} | "
            f"{row['visible_depth'] or 'n/a'} | {'; '.join(row['what_would_make_paper_ready'])} |"
        )
    if not payload["positive_ev_rows"]:
        lines.append("| _No positive-EV rows_ | | | | | | | | | |")
    return "\n".join(lines) + "\n"


def _render_phase3ap_refresh_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AP Positive-EV Book Refresh",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Status: {payload['status']}",
        f"- Dry run: {payload['dry_run']}",
        f"- Apply read-only refresh: {payload['apply_readonly_refresh']}",
        f"- Market-data writes: {payload['market_data_writes']}",
        "",
        "## Markets Needing Refresh",
        "",
    ]
    for ticker in payload["markets_needing_book_refresh"]:
        lines.append(f"- {ticker}")
    if not payload["markets_needing_book_refresh"]:
        lines.append("- none")
    lines.extend(["", "## Next Action", "", payload["next_action"], ""])
    return "\n".join(lines)


def _render_phase3ap_settlement_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AP Settlement Check Diagnostic",
        "",
        f"- Generated at: {payload['generated_at']}",
        "- Mode: PAPER / READ ONLY",
        f"- Legacy generic rows: {payload['summary']['legacy_settlement_check_failed_rows']}",
        f"- Generic remaining: {payload['summary']['generic_settlement_check_failed_remaining']}",
        "",
        "| Reason | Count |",
        "|---|---:|",
    ]
    for reason, count in sorted(payload["specific_reason_counts"].items()):
        lines.append(f"| {reason} | {count} |")
    if not payload["specific_reason_counts"]:
        lines.append("| _none_ | 0 |")
    lines.extend(["", "| Ticker | Specific reason | Blocks entry | Eligible | Detail |", "|---|---|---|---|---|"])
    for row in payload["rows"][:50]:
        lines.append(
            f"| {row['ticker']} | {row['specific_reason_code']} | {row['blocks_paper_entry']} | "
            f"{row['paper_entry_settlement_eligible']} | {row['reason']} |"
        )
    return "\n".join(lines) + "\n"


def _render_phase3ap_executive_summary(
    *,
    book: dict[str, Any],
    settlement: dict[str, Any],
    gate: dict[str, Any],
    phase3an: dict[str, Any],
) -> str:
    book_summary = book["summary"]
    gate_summary = gate["summary"]
    settlement_summary = settlement["summary"]
    no_book_reason = next(iter(book_summary["no_book_reason_counts"]), "none")
    fresh_executable = sum(
        1 for row in book["positive_ev_rows"]
        if row["executable_book"] and row["book_freshness_state"] == "FRESH"
    )
    general = phase3an.get("general_sources", {}).get("summary", {})
    sports = phase3an.get("sports", {}).get("summary", {})
    econ = phase3an.get("economic_news", {}).get("summary", {})
    lines = [
        "# Phase 3AP Executive Summary",
        "",
        f"- Generated at: {book['generated_at']}",
        "- Mode: PAPER / READ ONLY",
        "- Live/demo exchange writes: blocked",
        "- Paper trades created by this phase: 0",
        "- Thresholds changed: no",
        "",
        "## Answers",
        "",
        (
            "1. Positive-EV rows are blocked because the canonical paper-ready gate found "
            f"`{gate_summary['paper_ready_rows']}` paper-ready rows and "
            f"`{book_summary['positive_ev_no_executable_book_rows']}` positive-EV rows without executable books."
        ),
        (
            "2. Real Kalshi links are enforced by Phase 3AO identity fields; every paper-ready row "
            f"has verified links: `{gate_summary['all_paper_ready_have_verified_kalshi_links']}`."
        ),
        (
            "3. The leading book issue is "
            f"`{no_book_reason}`; fresh executable positive-EV rows currently: `{fresh_executable}`."
        ),
        (
            "4. Settlement check is split into specific reason codes; generic remaining: "
            f"`{settlement_summary['generic_settlement_check_failed_remaining']}`. "
            f"Open known-term rows eligible for paper entry: `{settlement_summary['open_market_entry_eligible_rows']}`."
        ),
        (
            "5. Rows that would become paper-ready after fresh executable book data: "
            f"`{_would_be_ready_after_book(book['positive_ev_rows'])}`."
        ),
        "6. Next single best command: `kalshi-bot phase3ap-refresh-positive-ev-books --dry-run --output-dir reports/phase3ap`.",
        "7. Thresholds should not be changed.",
        "8. No command in this phase created paper trades.",
        "",
        "## Source Gates",
        "",
        (
            f"- General sources: evidence-ready `{general.get('source_evidence_ready_rows', 0)}`, "
            f"link-safe `{general.get('link_safe_rows', 0)}`, forecast-safe `{general.get('forecast_safe_rows', 0)}`."
        ),
        (
            f"- Sports: placeholders `{sports.get('placeholder_rows', 0)}`, "
            f"partial provenance `{sports.get('partial_provenance_markets', 0)}`, "
            f"safe repair rows `{sports.get('safe_repair_rows', 0)}`."
        ),
        (
            f"- Economic/news: economic compatible `{econ.get('economic_compatible_parsed_markets', 0)}`, "
            f"news compatible `{econ.get('news_compatible_parsed_markets', 0)}`."
        ),
        "",
    ]
    return "\n".join(lines)


def _render_phase3ap_next_actions(
    *,
    book: dict[str, Any],
    settlement: dict[str, Any],
    gate: dict[str, Any],
) -> str:
    lines = [
        "# Phase 3AP Next Actions",
        "",
        "1. Keep the system paper/read-only.",
        "2. Run `kalshi-bot phase3ap-refresh-positive-ev-books --dry-run --output-dir reports/phase3ap`.",
        "3. If dry-run shows no active writer and the operator wants fresh book snapshots, run `kalshi-bot phase3ap-refresh-positive-ev-books --apply-readonly-refresh --max-markets 25 --max-duration-seconds 120 --output-dir reports/phase3ap`.",
        "4. Rerun `kalshi-bot phase3ap-paper-ready-unblock-report --output-dir reports/phase3ap --reports-dir reports`.",
        "5. Do not lower EV, confidence, liquidity, spread, quote freshness, settlement, or risk thresholds.",
        "",
        "## Current Top Blockers",
        "",
    ]
    for reason, count in gate["summary"]["reason_counts"].items():
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## Settlement Split", ""])
    for reason, count in settlement["specific_reason_counts"].items():
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## Book Split", ""])
    for reason, count in book["summary"]["no_book_reason_counts"].items():
        lines.append(f"- {reason}: {count}")
    return "\n".join(lines) + "\n"


def _would_be_ready_after_book(rows: list[dict[str, Any]]) -> int:
    return sum(
        1
        for row in rows
        if row["paper_ready_blocker"] in {"NO_EXECUTABLE_BOOK", "STALE_QUOTE"}
        and "PHASE_3N_RISK_BLOCK" not in row["secondary_blockers"]
        and "PHASE_3M_ZERO_SIZE" not in row["secondary_blockers"]
    )


def _phase3ap_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys() if key != "market_identity"})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def _write_manifest(path: Path, paths: list[Path]) -> None:
    lines = []
    for item in paths:
        if not item.exists():
            continue
        digest = __import__("hashlib").sha256(item.read_bytes()).hexdigest()
        lines.append(f"{digest}  {item.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
