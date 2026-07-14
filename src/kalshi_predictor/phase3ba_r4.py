from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.ticker_windows import crypto_ticker_close_time_utc
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    PaperPnl,
    PositionSizingDecisionLog,
)
from kalshi_predictor.opportunities.market_identity import verify_market_identity
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3bc import (
    MIN_EXECUTABLE_CONFIDENCE_SCORE,
    MIN_EXECUTABLE_LIQUIDITY_SCORE,
    MODEL_NAME,
    build_phase3bc_crypto_clean_opportunity_router,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE3BA_R4_VERSION = "phase3ba_r4_crypto_executable_book_watch_v1"

WATCH_STATES = (
    "POSITIVE_EV_NO_BOOK",
    "POSITIVE_EV_THIN_BOOK",
    "POSITIVE_EV_WIDE_SPREAD",
    "POSITIVE_EV_RISK_NOT_ELIGIBLE",
    "POSITIVE_EV_READY_FOR_RISK",
    "PAPER_READY",
)


@dataclass(frozen=True)
class Phase3BAR4ArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    json_path: Path
    positive_ev_csv_path: Path
    liquidity_watchlist_csv_path: Path
    reconciliation_sources_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3ba_r4_crypto_executable_book_watch_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r4"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = 2000,
) -> Phase3BAR4ArtifactSet:
    payload = build_phase3ba_r4_crypto_executable_book_watch(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    json_path = output_dir / "crypto_executable_book_watch.json"
    positive_ev_csv_path = output_dir / "crypto_positive_ev_rows.csv"
    liquidity_watchlist_csv_path = output_dir / "crypto_liquidity_watchlist.csv"
    reconciliation_sources_path = output_dir / "reconciliation_sources.json"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_rows_csv(positive_ev_csv_path, payload["positive_ev_rows"])
    _write_rows_csv(liquidity_watchlist_csv_path, payload["liquidity_watchlist_rows"])
    reconciliation_sources_path.write_text(
        json.dumps(payload["reconciliation_sources"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            json_path,
            positive_ev_csv_path,
            liquidity_watchlist_csv_path,
            reconciliation_sources_path,
            next_actions_path,
        ],
    )
    return Phase3BAR4ArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        json_path=json_path,
        positive_ev_csv_path=positive_ev_csv_path,
        liquidity_watchlist_csv_path=liquidity_watchlist_csv_path,
        reconciliation_sources_path=reconciliation_sources_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3ba_r4_crypto_executable_book_watch(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r4"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit: int = 2000,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    generated_at = utc_now()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=generated_at.isoformat(),
        command_args=command_args or [],
    )
    r5_status = _read_r5_status(reports_dir)
    router = build_phase3bc_crypto_clean_opportunity_router(
        session,
        settings=resolved,
        limit=limit,
    )
    rows = _positive_ev_current_rows(
        session,
        router.get("rows", []),
        settings=resolved,
        now=generated_at,
    )
    selected_source = "DB_ROWS"
    if not rows and _to_int(r5_status.get("positive_ev_rows")) > 0:
        rows = [_r5_aggregate_positive_ev_row(r5_status)]
        selected_source = "R5_STATUS_JSON"
    rows.sort(key=_sort_key, reverse=True)
    liquidity_watchlist = [
        row
        for row in rows
        if row["watch_state"]
        in {
            "POSITIVE_EV_NO_BOOK",
            "POSITIVE_EV_THIN_BOOK",
            "POSITIVE_EV_WIDE_SPREAD",
        }
    ]
    summary = _summary(rows, liquidity_watchlist=liquidity_watchlist)
    status = _status(summary)
    reconciliation_sources = _reconciliation_sources(
        selected_source=selected_source,
        r5_status=r5_status,
        rows=rows,
        router_summary=router.get("summary", {}),
    )
    return {
        **metadata,
        "phase": "3BA-R4",
        "phase_version": PHASE3BA_R4_VERSION,
        "mode": "PAPER_READ_ONLY_CRYPTO_EXECUTABLE_BOOK_WATCH",
        "status": status,
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "parameters": {
            "limit": limit,
            "model_name": MODEL_NAME,
        },
        "thresholds": {
            "min_executable_liquidity_score": str(MIN_EXECUTABLE_LIQUIDITY_SCORE),
            "max_spread": str(resolved.opportunity_max_spread),
            "min_edge": str(resolved.opportunity_min_edge),
            "min_score": str(resolved.opportunity_min_score),
            "min_confidence_score": str(MIN_EXECUTABLE_CONFIDENCE_SCORE),
            "min_time_to_close_minutes": str(
                resolved.opportunity_min_time_to_close_minutes
            ),
        },
        "router_summary": router.get("summary", {}),
        "r5_background_status": r5_status,
        "reconciliation_sources": reconciliation_sources,
        "r5_watcher_untouched": True,
        "watch_states": list(WATCH_STATES),
        "summary": summary,
        "watch_state_counts": dict(Counter(row["watch_state"] for row in rows)),
        "execution_blocker_detail_counts": dict(
            Counter(row["execution_blocker_detail"] for row in rows)
        ),
        "positive_ev_rows": rows,
        "liquidity_watchlist_rows": liquidity_watchlist,
        "acceptance": _acceptance(summary=summary, r5_status=r5_status),
        "next_action": _next_action(summary),
        "operator_guardrails": _operator_guardrails(),
    }


def _positive_ev_current_rows(
    session: Session,
    router_rows: list[dict[str, Any]],
    *,
    settings: Settings,
    now: Any,
) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in router_rows
        if row.get("structure_status") == "PURE_CRYPTO"
        and row.get("active_market")
        and _positive_ev(row)
        and _current_crypto_window(row, now=now)
    ]
    tickers = sorted({str(row.get("ticker")) for row in candidates if row.get("ticker")})
    markets = _markets_by_ticker(session, tickers)
    snapshots = _latest_by_ticker(session, MarketSnapshot, tickers, "captured_at")
    rankings = _latest_by_ticker(session, MarketRanking, tickers, "ranked_at")
    sizing = _latest_by_ticker(session, PositionSizingDecisionLog, tickers, "decision_timestamp")
    risk = _latest_by_ticker(session, AdvancedRiskDecisionLog, tickers, "decision_timestamp")
    return [
        _crypto_positive_ev_row(
            session,
            row,
            market=markets.get(str(row.get("ticker"))),
            snapshot=snapshots.get(str(row.get("ticker"))),
            ranking=rankings.get(str(row.get("ticker"))),
            sizing=sizing.get(str(row.get("ticker"))),
            risk=risk.get(str(row.get("ticker"))),
            settings=settings,
            now=now,
        )
        for row in candidates
    ]


def _crypto_positive_ev_row(
    session: Session,
    row: dict[str, Any],
    *,
    market: Market | None,
    snapshot: MarketSnapshot | None,
    ranking: MarketRanking | None,
    sizing: PositionSizingDecisionLog | None,
    risk: AdvancedRiskDecisionLog | None,
    settings: Settings,
    now: Any,
) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "")
    identity = verify_market_identity(
        session,
        ticker=ticker,
        ranking=ranking,
        market=market,
        settings=settings,
    )
    identity_payload = identity.as_dict()
    identity_payload["r4_url_gate_pass"] = _url_gate_pass(identity_payload)
    liquidity_score = to_decimal(row.get("liquidity_score"))
    spread = to_decimal(row.get("spread"))
    score = to_decimal(row.get("opportunity_score"))
    confidence = to_decimal(row.get("confidence_score"))
    time_to_close = to_decimal(row.get("time_to_close_minutes"))
    phase3m_contracts = int(getattr(sizing, "proposed_contracts", 0) or 0)
    risk_action = str(getattr(risk, "action", "") or "").upper()
    risk_approved = risk_action in {"ALLOW", "APPROVE", "PROCEED"}
    execution = _execution_assessment(
        row,
        identity=identity_payload,
        snapshot=snapshot,
        liquidity_score=liquidity_score,
        spread=spread,
        score=score,
        confidence=confidence,
        time_to_close=time_to_close,
        phase3m_contracts=phase3m_contracts,
        risk_approved=risk_approved,
        settings=settings,
    )
    return {
        "ticker": ticker,
        "clean_title": row.get("clean_title"),
        "event_ticker": row.get("event_ticker"),
        "series_ticker": row.get("series_ticker"),
        "market_status": row.get("market_status"),
        "active_market": bool(row.get("active_market")),
        "current_market": _current_crypto_window(row, now=now),
        "verified_kalshi_url": bool(identity_payload.get("tradeable")),
        "clickable_kalshi_url": bool(identity_payload.get("kalshi_url_verified")),
        "exact_catalog_or_verified_link": bool(identity_payload.get("r4_url_gate_pass")),
        "kalshi_url": identity_payload.get("kalshi_url"),
        "kalshi_url_status": identity_payload.get("kalshi_url_status"),
        "latest_snapshot_at": row.get("latest_snapshot_at"),
        "latest_forecast_at": row.get("latest_forecast_at"),
        "latest_ranking_at": row.get("latest_ranking_at"),
        "snapshot_present": snapshot is not None,
        "orderbook_present": bool(snapshot is not None and snapshot.raw_orderbook_json),
        "best_side": row.get("best_side"),
        "best_price": row.get("best_price"),
        "expected_value": row.get("expected_value"),
        "expected_value_cents": _cents(to_decimal(row.get("expected_value"))),
        "estimated_edge": row.get("estimated_edge"),
        "opportunity_score": decimal_to_str(score),
        "confidence_score": decimal_to_str(confidence),
        "liquidity_score": decimal_to_str(liquidity_score),
        "liquidity": row.get("liquidity"),
        "spread": decimal_to_str(spread),
        "book_state": row.get("book_state"),
        "book_usable": bool(row.get("book_usable")),
        "book_reason": row.get("book_reason"),
        "book_bid_price": row.get("book_bid_price"),
        "book_ask_price": row.get("book_ask_price"),
        "book_spread": row.get("book_spread"),
        "bid_depth": row.get("bid_depth"),
        "ask_depth": row.get("ask_depth"),
        "executable_side_present": execution["executable_side_present"],
        "liquidity_pass": execution["liquidity_pass"],
        "spread_pass": execution["spread_pass"],
        "score_pass": execution["score_pass"],
        "confidence_pass": execution["confidence_pass"],
        "time_pass": execution["time_pass"],
        "risk_eligible": execution["risk_eligible"],
        "phase3m_nonzero_size": phase3m_contracts > 0,
        "phase3m_proposed_contracts": phase3m_contracts,
        "phase3n_approved": risk_approved,
        "phase3n_action": risk_action or None,
        "watch_state": execution["watch_state"],
        "execution_blocker_detail": execution["execution_blocker_detail"],
        "what_would_make_paper_ready": execution["what_would_make_paper_ready"],
    }


def _execution_assessment(
    row: dict[str, Any],
    *,
    identity: dict[str, Any],
    snapshot: MarketSnapshot | None,
    liquidity_score: Decimal | None,
    spread: Decimal | None,
    score: Decimal | None,
    confidence: Decimal | None,
    time_to_close: Decimal | None,
    phase3m_contracts: int,
    risk_approved: bool,
    settings: Settings,
) -> dict[str, Any]:
    best_price = to_decimal(row.get("best_price"))
    book_state = str(row.get("book_state") or "")
    executable_side_present = bool(row.get("best_side") and best_price is not None)
    liquidity_pass = bool(
        liquidity_score is not None and liquidity_score >= MIN_EXECUTABLE_LIQUIDITY_SCORE
    )
    spread_pass = bool(spread is not None and spread <= settings.opportunity_max_spread)
    score_pass = bool(score is not None and score >= settings.opportunity_min_score)
    confidence_pass = bool(
        confidence is None or confidence >= MIN_EXECUTABLE_CONFIDENCE_SCORE
    )
    time_pass = bool(
        time_to_close is None or time_to_close >= settings.opportunity_min_time_to_close_minutes
    )
    if not identity.get("r4_url_gate_pass"):
        return _assessment(
            "POSITIVE_EV_RISK_NOT_ELIGIBLE",
            "URL_NOT_VERIFIED",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Repair/verify exact Kalshi URL before this can enter paper review.",
        )
    if snapshot is None:
        return _assessment(
            "POSITIVE_EV_NO_BOOK",
            "SNAPSHOT_MISSING",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Wait for/capture a fresh market snapshot.",
        )
    if not snapshot.raw_orderbook_json:
        return _assessment(
            "POSITIVE_EV_NO_BOOK",
            "ORDERBOOK_MISSING",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Wait for an orderbook snapshot with visible bid/ask depth.",
        )
    if not executable_side_present:
        return _assessment(
            "POSITIVE_EV_NO_BOOK",
            "EXECUTABLE_SIDE_MISSING",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Wait for a valid YES/NO executable side and price.",
        )
    if book_state == "NO_EXECUTABLE_BOOK" or not row.get("book_usable"):
        detail = _no_book_detail(row)
        return _assessment(
            "POSITIVE_EV_NO_BOOK",
            detail,
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Wait for visible executable depth at the best actionable price.",
        )
    if book_state == "THIN_BOOK" or not liquidity_pass:
        return _assessment(
            "POSITIVE_EV_THIN_BOOK",
            "LIQUIDITY_BELOW_THRESHOLD",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Wait for liquidity score/depth to rise above the configured threshold.",
        )
    if book_state == "WIDE_SPREAD" or not spread_pass:
        return _assessment(
            "POSITIVE_EV_WIDE_SPREAD",
            "SPREAD_ABOVE_THRESHOLD",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Wait for the spread to tighten below the configured threshold.",
        )
    if not score_pass:
        return _assessment(
            "POSITIVE_EV_RISK_NOT_ELIGIBLE",
            "SCORE_BELOW_THRESHOLD",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Wait for score to clear the paper-ready threshold.",
        )
    if not confidence_pass:
        return _assessment(
            "POSITIVE_EV_RISK_NOT_ELIGIBLE",
            "CONFIDENCE_BELOW_THRESHOLD",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Wait for model confidence to clear the executable threshold.",
        )
    if not time_pass:
        return _assessment(
            "POSITIVE_EV_RISK_NOT_ELIGIBLE",
            "TOO_CLOSE_TO_SETTLEMENT",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Wait for a future window with enough time before close.",
        )
    risk_eligible = score_pass and confidence_pass and time_pass
    if phase3m_contracts <= 0:
        return _assessment(
            "POSITIVE_EV_READY_FOR_RISK",
            "AWAITING_PHASE_3M_NONZERO_SIZE",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Run paper-only Phase 3M/3N preflight; do not create trades.",
            risk_eligible=risk_eligible,
        )
    if not risk_approved:
        return _assessment(
            "POSITIVE_EV_RISK_NOT_ELIGIBLE",
            "PHASE_3N_NOT_APPROVED",
            executable_side_present,
            liquidity_pass,
            spread_pass,
            score_pass,
            confidence_pass,
            time_pass,
            "Wait for an approving paper-only Phase 3N risk decision.",
            risk_eligible=False,
        )
    return _assessment(
        "PAPER_READY",
        "ALL_EXECUTION_AND_RISK_GATES_PASS",
        executable_side_present,
        liquidity_pass,
        spread_pass,
        score_pass,
        confidence_pass,
        time_pass,
        "Paper-ready candidate; operator review only, no trade creation in R4.",
        risk_eligible=True,
    )


def _assessment(
    watch_state: str,
    detail: str,
    executable_side_present: bool,
    liquidity_pass: bool,
    spread_pass: bool,
    score_pass: bool,
    confidence_pass: bool,
    time_pass: bool,
    action: str,
    *,
    risk_eligible: bool | None = None,
) -> dict[str, Any]:
    return {
        "watch_state": watch_state,
        "execution_blocker_detail": detail,
        "executable_side_present": executable_side_present,
        "liquidity_pass": liquidity_pass,
        "spread_pass": spread_pass,
        "score_pass": score_pass,
        "confidence_pass": confidence_pass,
        "time_pass": time_pass,
        "risk_eligible": bool(
            risk_eligible
            if risk_eligible is not None
            else score_pass and confidence_pass and time_pass
        ),
        "what_would_make_paper_ready": [action],
    }


def _no_book_detail(row: dict[str, Any]) -> str:
    reason = str(row.get("book_reason") or "").upper()
    if "EMPTY" in reason:
        return "EMPTY_ORDERBOOK"
    if "VISIBLE" in reason or "ZERO" in reason:
        return "ZERO_VISIBLE_DEPTH"
    if "DEPTH" in reason or "LIQUIDITY" in reason:
        return "NO_EXECUTABLE_DEPTH"
    return "NO_USABLE_BID_ASK_BOOK"


def _url_gate_pass(identity: dict[str, Any]) -> bool:
    status = str(
        identity.get("kalshi_url_status")
        or identity.get("url_verification_status")
        or ""
    )
    if status == "BUILT_FROM_EXACT_CATALOG":
        return True
    return bool(identity.get("tradeable") or status.startswith("VERIFIED"))


def _summary(
    rows: list[dict[str, Any]],
    *,
    liquidity_watchlist: list[dict[str, Any]],
) -> dict[str, Any]:
    watch_counts = _weighted_counter(rows, "watch_state")
    detail_counts = _weighted_counter(rows, "execution_blocker_detail")
    positive_ev_rows = sum(_positive_ev_row_count(row) for row in rows)
    paper_ready_rows = sum(_paper_ready_row_count(row) for row in rows)
    no_book_rows = sum(
        _to_int(row.get("aggregate_positive_ev_no_executable_book_rows"))
        if _is_r5_aggregate_row(row)
        else int(row.get("watch_state") == "POSITIVE_EV_NO_BOOK")
        for row in rows
    )
    return {
        "positive_ev_rows": positive_ev_rows,
        "paper_ready_rows": paper_ready_rows,
        "paper_ready_candidates": paper_ready_rows,
        "positive_ev_no_book_rows": no_book_rows,
        "positive_ev_no_executable_book_rows": no_book_rows,
        "positive_ev_thin_book_rows": watch_counts.get("POSITIVE_EV_THIN_BOOK", 0),
        "positive_ev_wide_spread_rows": watch_counts.get("POSITIVE_EV_WIDE_SPREAD", 0),
        "positive_ev_risk_not_eligible_rows": watch_counts.get(
            "POSITIVE_EV_RISK_NOT_ELIGIBLE",
            0,
        ),
        "positive_ev_ready_for_risk_rows": watch_counts.get(
            "POSITIVE_EV_READY_FOR_RISK",
            0,
        ),
        "liquidity_watchlist_rows": len(liquidity_watchlist),
        "exact_execution_blockers_reported": all(
            bool(row.get("execution_blocker_detail")) for row in rows
        ),
        "watch_state_counts": dict(watch_counts),
        "execution_blocker_detail_counts": dict(detail_counts),
        "primary_watch_state": watch_counts.most_common(1)[0][0] if watch_counts else None,
        "r5_aggregate_truth_only": any(_is_r5_aggregate_row(row) for row in rows),
    }


def _status(summary: dict[str, Any]) -> str:
    if summary["paper_ready_rows"] > 0:
        return "CRYPTO_PAPER_READY_REVIEW"
    if summary["positive_ev_ready_for_risk_rows"] > 0:
        return "CRYPTO_READY_FOR_RISK_PREFLIGHT"
    if summary.get("positive_ev_no_executable_book_rows", 0) > 0:
        return "CRYPTO_POSITIVE_EV_BLOCKED_BY_EXECUTABLE_BOOK"
    if summary["positive_ev_rows"] > 0:
        return "CRYPTO_POSITIVE_EV_EXECUTION_BLOCKED"
    return "CRYPTO_WAITING_FOR_POSITIVE_EV"


def _r5_aggregate_positive_ev_row(r5_status: dict[str, Any]) -> dict[str, Any]:
    positive_ev_rows = _to_int(r5_status.get("positive_ev_rows"))
    no_book_rows = _to_int(
        r5_status.get("positive_ev_no_executable_book_rows")
        or r5_status.get("positive_ev_no_book_rows")
    )
    clean_execution_rows = _to_int(r5_status.get("clean_execution_rows"))
    risk_ready_rows = _to_int(r5_status.get("risk_ready_rows"))
    paper_ready = _to_int(r5_status.get("paper_ready_candidates"))
    primary_gap = r5_status.get("primary_gap_after_refresh")
    watch_state = "POSITIVE_EV_NO_BOOK" if no_book_rows > 0 else "POSITIVE_EV_RISK_NOT_ELIGIBLE"
    detail = "POSITIVE_EV_NO_EXECUTABLE_BOOK" if no_book_rows > 0 else str(
        primary_gap or "R5_AGGREGATE_POSITIVE_EV_BLOCKED"
    )
    return {
        "ticker": None,
        "clean_title": "R5 aggregate crypto truth; row-level opportunity detail unavailable",
        "event_ticker": None,
        "series_ticker": None,
        "market_status": None,
        "active_market": True,
        "current_market": True,
        "verified_kalshi_url": None,
        "clickable_kalshi_url": None,
        "exact_catalog_or_verified_link": None,
        "kalshi_url": None,
        "kalshi_url_status": "R5_AGGREGATE_TRUTH_ONLY",
        "latest_snapshot_at": None,
        "latest_forecast_at": None,
        "latest_ranking_at": r5_status.get("latest_report_generated_at"),
        "snapshot_present": None,
        "orderbook_present": no_book_rows <= 0,
        "best_side": None,
        "best_price": None,
        "expected_value": None,
        "expected_value_cents": r5_status.get("best_current_expected_value_cents"),
        "estimated_edge": None,
        "opportunity_score": None,
        "confidence_score": None,
        "liquidity_score": None,
        "liquidity": None,
        "spread": None,
        "book_state": "NO_EXECUTABLE_BOOK" if no_book_rows > 0 else primary_gap,
        "book_usable": no_book_rows <= 0 and clean_execution_rows > 0,
        "book_reason": detail,
        "book_bid_price": None,
        "book_ask_price": None,
        "book_spread": None,
        "bid_depth": None,
        "ask_depth": None,
        "executable_side_present": clean_execution_rows > 0,
        "liquidity_pass": clean_execution_rows > 0,
        "spread_pass": clean_execution_rows > 0,
        "score_pass": primary_gap != "LOW_EDGE_OR_SCORE_BLOCK",
        "confidence_pass": None,
        "time_pass": None,
        "risk_eligible": risk_ready_rows > 0,
        "phase3m_nonzero_size": False,
        "phase3m_proposed_contracts": 0,
        "phase3n_approved": False,
        "phase3n_action": None,
        "watch_state": watch_state,
        "execution_blocker_detail": detail,
        "primary_gap_after_refresh": primary_gap,
        "source": "R5_STATUS_JSON",
        "evidence_scope": "R5_AGGREGATE_TRUTH_ONLY",
        "aggregate_positive_ev_rows": positive_ev_rows,
        "aggregate_positive_ev_no_executable_book_rows": no_book_rows,
        "aggregate_clean_execution_rows": clean_execution_rows,
        "aggregate_risk_ready_rows": risk_ready_rows,
        "aggregate_paper_ready_candidates": paper_ready,
        "best_ev_candidate_ticker": r5_status.get("best_ev_candidate_ticker"),
        "what_would_make_paper_ready": [
            "R5 reported aggregate positive-EV crypto rows, but row-level opportunity "
            "records were unavailable. Keep R5 running and refresh materialized diagnostics."
        ],
    }


def _weighted_counter(rows: list[dict[str, Any]], field: str) -> Counter:
    counter: Counter = Counter()
    for row in rows:
        value = row.get(field)
        if value is not None:
            counter[str(value)] += _positive_ev_row_count(row)
    return counter


def _positive_ev_row_count(row: dict[str, Any]) -> int:
    if _is_r5_aggregate_row(row):
        return max(0, _to_int(row.get("aggregate_positive_ev_rows")))
    return 1


def _paper_ready_row_count(row: dict[str, Any]) -> int:
    if _is_r5_aggregate_row(row):
        return max(0, _to_int(row.get("aggregate_paper_ready_candidates")))
    return int(row.get("watch_state") == "PAPER_READY")


def _is_r5_aggregate_row(row: dict[str, Any]) -> bool:
    return row.get("evidence_scope") == "R5_AGGREGATE_TRUTH_ONLY"


def _reconciliation_sources(
    *,
    selected_source: str,
    r5_status: dict[str, Any],
    rows: list[dict[str, Any]],
    router_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "crypto": {
            "source_precedence": [
                "CURRENT_DB_ROWS",
                "R5_STATUS_JSON",
                "STALE_REPORTS_DIAGNOSTIC_ONLY",
            ],
            "selected_source": selected_source,
            "aggregate_truth_only": any(_is_r5_aggregate_row(row) for row in rows),
            "r5_status_path": r5_status.get("status_path"),
            "r5_status_loaded": r5_status.get("loaded"),
            "r5_status_generated_at": r5_status.get("status_generated_at"),
            "r5_latest_summary_loaded": bool(r5_status.get("latest_summary")),
            "r5_guard_loaded": bool(r5_status.get("guard")),
            "r5_positive_ev_rows": r5_status.get("positive_ev_rows"),
            "r5_positive_ev_no_executable_book_rows": r5_status.get(
                "positive_ev_no_executable_book_rows"
            ),
            "r5_paper_ready_candidates": r5_status.get("paper_ready_candidates"),
            "r5_primary_gap_after_refresh": r5_status.get("primary_gap_after_refresh"),
            "router_summary": router_summary,
        }
    }


def _acceptance(*, summary: dict[str, Any], r5_status: dict[str, Any]) -> dict[str, Any]:
    return {
        "all_positive_ev_rows_have_exact_execution_blockers": bool(
            summary["positive_ev_rows"] == 0 or summary["exact_execution_blockers_reported"]
        ),
        "no_executable_book_split_into_subreasons": True,
        "risk_eligible_rows_marked": True,
        "no_paper_trades_created": True,
        "no_live_or_demo_orders": True,
        "r5_continues_background_watcher": _r5_running(r5_status),
        "duplicate_r5_watcher_started": False,
    }


def _next_action(summary: dict[str, Any]) -> dict[str, Any]:
    if summary["paper_ready_rows"] > 0:
        return {
            "stage": "PAPER_ONLY_OPERATOR_REVIEW",
            "command": (
                "kalshi-bot phase3ap-paper-ready-unblock-report --output-dir "
                "reports/phase3ap --reports-dir reports"
            ),
            "reason": "Crypto has paper-ready rows; refresh 3AP and review only.",
            "allow_paper_trade_creation": False,
        }
    if summary["positive_ev_ready_for_risk_rows"] > 0:
        return {
            "stage": "RUN_PAPER_ONLY_RISK_PREFLIGHT",
            "command": (
                "kalshi-bot phase3bc-r16-crypto-paper-ready-edge-hunt "
                "--output-dir reports/phase3bc_r16 --max-preflight 5"
            ),
            "reason": "Clean positive-EV book exists; run paper-only risk preflight.",
            "allow_paper_trade_creation": False,
        }
    return {
        "stage": "KEEP_R5_BACKGROUND_WATCH",
        "command": (
            "kalshi-bot phase3ba-r4-crypto-executable-book-watch --output-dir "
            "reports/phase3ba_r4 --reports-dir reports"
        ),
        "reason": "Positive-EV crypto rows still need executable book/liquidity/spread.",
        "allow_paper_trade_creation": False,
    }


def _operator_guardrails() -> list[str]:
    return [
        "Keep PAPER / READ-ONLY.",
        "Do not submit, cancel, replace, or amend live/demo exchange orders.",
        "Do not create paper trades from this phase.",
        "Do not fabricate liquidity, books, fills, or executable prices.",
        "Do not lower liquidity, spread, EV, score, confidence, or risk thresholds.",
        "Do not start duplicate R5 watchers.",
    ]


def _read_r5_status(reports_dir: Path) -> dict[str, Any]:
    status_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json"
    watch_path = reports_dir / "phase3bc_r5" / "phase3bc_r5_crypto_freshness_watch.json"
    status = _read_json_if_exists(status_path)
    watch = _read_json_if_exists(watch_path)
    latest_summary = status.get("latest_summary") or {}
    guard = status.get("guard") or {}
    status_summary = status.get("summary") or {}
    watch_summary = watch.get("summary") or {}
    return {
        "status_path": str(status_path),
        "watch_path": str(watch_path),
        "status_generated_at": status.get("generated_at"),
        "watch_generated_at": watch.get("generated_at"),
        "latest_report_generated_at": status.get("latest_report_generated_at")
        or watch.get("generated_at"),
        "latest_summary": latest_summary,
        "guard": guard,
        "history_rows": status.get("history_rows"),
        "process_status": (status.get("process") or {}).get("status")
        or status.get("process_status"),
        "guard_status": guard.get("status") or status.get("guard_status"),
        "pid": (status.get("process") or {}).get("pid") or status.get("pid"),
        "watch_state": _first_present(
            status.get("latest_watch_state"),
            latest_summary.get("watch_state"),
            guard.get("watch_state"),
            status_summary.get("watch_state"),
            watch_summary.get("watch_state"),
        ),
        "paper_ready_candidates": _first_present(
            latest_summary.get("paper_ready_candidates"),
            guard.get("paper_ready_candidates"),
            status_summary.get("paper_ready_candidates"),
            watch_summary.get("paper_ready_candidates"),
        ),
        "positive_ev_rows": _first_present(
            latest_summary.get("positive_ev_rows"),
            guard.get("positive_ev_rows"),
            status_summary.get("positive_ev_rows"),
            watch_summary.get("positive_ev_rows"),
        ),
        "positive_ev_no_executable_book_rows": _first_present(
            latest_summary.get("positive_ev_no_executable_book_rows"),
            guard.get("positive_ev_no_executable_book_rows"),
            latest_summary.get("positive_ev_no_book_rows"),
            status_summary.get("positive_ev_no_executable_book_rows"),
            watch_summary.get("positive_ev_no_executable_book_rows"),
        ),
        "clean_execution_rows": _first_present(
            latest_summary.get("clean_execution_rows"),
            guard.get("clean_execution_rows"),
            status_summary.get("clean_execution_rows"),
            watch_summary.get("clean_execution_rows"),
        ),
        "risk_ready_rows": _first_present(
            latest_summary.get("risk_ready_rows"),
            guard.get("risk_ready_rows"),
            status_summary.get("risk_ready_rows"),
            watch_summary.get("risk_ready_rows"),
        ),
        "primary_gap_after_refresh": _first_present(
            latest_summary.get("primary_gap_after_refresh"),
            guard.get("primary_gap_after_refresh"),
            status_summary.get("primary_gap_after_refresh"),
            watch_summary.get("primary_gap_after_refresh"),
        ),
        "best_ev_candidate_ticker": _first_present(
            latest_summary.get("best_ev_candidate_ticker"),
            guard.get("best_ev_candidate_ticker"),
            status_summary.get("best_ev_candidate_ticker"),
            watch_summary.get("best_ev_candidate_ticker"),
        ),
        "best_current_expected_value_cents": _first_present(
            latest_summary.get("best_current_expected_value_cents"),
            latest_summary.get("best_ev_cents"),
            guard.get("best_current_expected_value_cents"),
            status_summary.get("best_current_expected_value_cents"),
            watch_summary.get("best_current_expected_value_cents"),
        ),
        "loaded": bool(status or watch),
    }


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _to_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _r5_running(r5_status: dict[str, Any]) -> bool:
    values = {
        str(r5_status.get("process_status") or "").upper(),
        str(r5_status.get("guard_status") or "").upper(),
    }
    return "RUNNING" in values


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _positive_ev(row: dict[str, Any]) -> bool:
    value = to_decimal(row.get("expected_value"))
    return value is not None and value > 0


def _current_crypto_window(row: dict[str, Any], *, now: Any) -> bool:
    close_time = crypto_ticker_close_time_utc(str(row.get("ticker") or ""))
    return close_time is None or close_time > now


def _sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    watch_priority = {
        "PAPER_READY": Decimal("6"),
        "POSITIVE_EV_READY_FOR_RISK": Decimal("5"),
        "POSITIVE_EV_RISK_NOT_ELIGIBLE": Decimal("4"),
        "POSITIVE_EV_WIDE_SPREAD": Decimal("3"),
        "POSITIVE_EV_THIN_BOOK": Decimal("2"),
        "POSITIVE_EV_NO_BOOK": Decimal("1"),
    }.get(str(row.get("watch_state")), Decimal("0"))
    return (
        watch_priority,
        to_decimal(row.get("expected_value")) or Decimal("-999"),
        to_decimal(row.get("liquidity_score")) or Decimal("0"),
        to_decimal(row.get("opportunity_score")) or Decimal("0"),
    )


def _markets_by_ticker(session: Session, tickers: list[str]) -> dict[str, Market]:
    if not tickers:
        return {}
    return {
        row.ticker: row
        for row in session.scalars(select(Market).where(Market.ticker.in_(tickers)))
    }


def _latest_by_ticker(
    session: Session,
    model: Any,
    tickers: list[str],
    time_attr: str,
) -> dict[str, Any]:
    if not tickers:
        return {}
    column = getattr(model, time_attr)
    statement = select(model).where(model.ticker.in_(tickers))
    if model is MarketRanking:
        statement = statement.where(MarketRanking.forecast_model == MODEL_NAME)
    rows = list(
        session.scalars(
            statement.order_by(
                model.ticker,
                desc(column),
                desc(model.id) if hasattr(model, "id") else desc(column),
            )
        )
    )
    latest: dict[str, Any] = {}
    for row in rows:
        latest.setdefault(row.ticker, row)
    return latest


def _metadata(
    session: Session,
    *,
    settings: Settings,
    generated_at: str,
    command_args: list[str],
) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    return {
        "generated_at": generated_at,
        "repository_root": str(Path.cwd().resolve()),
        "git_branch": _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": _git_dirty_status(),
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(__file__).resolve()),
        "resolved_database_url": redact_database_url(db_url),
        "database_fingerprint": _database_fingerprint(db_url),
        "database_location": describe_db_location(db_url),
        "migration_revision": _migration_revision(session),
        "timezone": getattr(settings, "timezone", None) or "UTC",
        "command_arguments": {
            "command": "kalshi-bot phase3ba-r4-crypto-executable-book-watch",
            "argv": command_args,
        },
        "data_watermark": _data_watermark(session),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "safety_flags": _safety_flags(),
    }


def _data_watermark(session: Session) -> dict[str, Any]:
    return {
        "latest_market_seen_at": _latest_iso(session, Market.last_seen_at),
        "latest_snapshot_at": _latest_iso(session, MarketSnapshot.captured_at),
        "latest_crypto_v2_ranking_at": _latest_crypto_ranking_iso(session),
        "latest_paper_order_at": _latest_iso(session, PaperOrder.created_at),
        "latest_paper_pnl_at": _latest_iso(session, PaperPnl.calculated_at),
    }


def _latest_iso(session: Session, column: Any) -> str | None:
    value = session.scalar(select(func.max(column)))
    return value.isoformat() if hasattr(value, "isoformat") else value


def _latest_crypto_ranking_iso(session: Session) -> str | None:
    value = session.scalar(
        select(func.max(MarketRanking.ranked_at)).where(
            MarketRanking.forecast_model == MODEL_NAME
        )
    )
    return value.isoformat() if hasattr(value, "isoformat") else value


def _database_fingerprint(db_url: str) -> dict[str, Any]:
    redacted = redact_database_url(db_url)
    sqlite_path = sqlite_path_from_url(db_url)
    if sqlite_path is None:
        return {
            "kind": "non_sqlite",
            "database_url_hash": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
        }
    if str(sqlite_path) == ":memory:":
        return {"kind": "sqlite_memory", "path": ":memory:"}
    path = sqlite_path.expanduser().resolve()
    if not path.exists():
        return {"kind": "missing_sqlite_file", "path": str(path)}
    stat = path.stat()
    payload = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    return {
        "kind": "sqlite_file_stat",
        **payload,
        "fingerprint": hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def _migration_revision(session: Session) -> str | None:
    try:
        return session.execute(text("select version_num from alembic_version limit 1")).scalar()
    except Exception:
        return None


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


def _safety_flags() -> dict[str, Any]:
    return {
        "paper_only": True,
        "diagnostic_only": True,
        "creates_rankings": False,
        "creates_opportunity_rows": False,
        "creates_paper_orders": False,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "live_trading_enabled": False,
        "demo_exchange_writes_enabled": False,
        "thresholds_lowered": False,
        "fabricates_liquidity": False,
        "fabricates_books": False,
        "fabricates_fills": False,
        "fabricates_executable_prices": False,
        "starts_r5_watcher": False,
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R4 Crypto Executable Book Watch")
    summary = payload["summary"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{payload['status']}`",
            f"- Positive-EV rows: `{summary['positive_ev_rows']}`",
            f"- No-book rows: `{summary['positive_ev_no_book_rows']}`",
            f"- Thin-book rows: `{summary['positive_ev_thin_book_rows']}`",
            f"- Wide-spread rows: `{summary['positive_ev_wide_spread_rows']}`",
            f"- Ready-for-risk rows: `{summary['positive_ev_ready_for_risk_rows']}`",
            f"- Paper-ready rows: `{summary['paper_ready_rows']}`",
            f"- Primary watch state: `{summary['primary_watch_state']}`",
            "",
            "## Next Action",
            "",
            f"- Stage: `{payload['next_action']['stage']}`",
            f"- Command: `{payload['next_action']['command']}`",
            f"- Paper trade creation allowed: "
            f"`{payload['next_action']['allow_paper_trade_creation']}`",
            f"- Reason: {payload['next_action']['reason']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R4 Next Actions")
    action = payload["next_action"]
    lines.extend(
        [
            "",
            "## Exact Next Operator Command",
            "",
            f"```bash\n{action['command']}\n```",
            "",
            f"- Stage: `{action['stage']}`",
            f"- Paper trade creation allowed: `{action['allow_paper_trade_creation']}`",
            f"- Reason: {action['reason']}",
            "",
            "## R5 Background Watcher",
            "",
            f"- R5 watcher untouched: `{payload['r5_watcher_untouched']}`",
            f"- R5 process status: `{payload['r5_background_status'].get('process_status')}`",
            f"- R5 guard status: `{payload['r5_background_status'].get('guard_status')}`",
            "",
            "## Guardrails",
            "",
        ]
    )
    for guardrail in payload["operator_guardrails"]:
        lines.append(f"- {guardrail}")
    return "\n".join(lines) + "\n"


def _metadata_lines(payload: dict[str, Any], *, title: str) -> list[str]:
    return [
        title,
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Git commit: `{payload['git_commit']}`",
        f"- DB fingerprint: `{json.dumps(payload['database_fingerprint'], sort_keys=True)}`",
        f"- Data watermark: `{json.dumps(payload['data_watermark'], sort_keys=True)}`",
        f"- Command args: `{json.dumps(payload['command_arguments'], sort_keys=True)}`",
        f"- Safety flags: `{json.dumps(payload['safety_flags'], sort_keys=True)}`",
        f"- Live/demo execution: `{payload['live_or_demo_execution']}`",
        "- Order submission/cancel/replace: "
        f"`{payload['order_submission'] or payload['order_cancel_replace']}`",
        f"- Paper trade creation: `{payload['paper_trade_creation']}`",
        f"- Thresholds lowered: `{payload['thresholds_lowered']}`",
    ]


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "ticker",
        "clean_title",
        "watch_state",
        "execution_blocker_detail",
        "expected_value",
        "expected_value_cents",
        "best_side",
        "best_price",
        "book_state",
        "book_usable",
        "book_reason",
        "liquidity_score",
        "spread",
        "bid_depth",
        "ask_depth",
        "opportunity_score",
        "confidence_score",
        "verified_kalshi_url",
        "exact_catalog_or_verified_link",
        "kalshi_url_status",
        "snapshot_present",
        "orderbook_present",
        "executable_side_present",
        "liquidity_pass",
        "spread_pass",
        "score_pass",
        "risk_eligible",
        "phase3m_nonzero_size",
        "phase3m_proposed_contracts",
        "phase3n_approved",
        "phase3n_action",
        "latest_snapshot_at",
        "latest_forecast_at",
        "latest_ranking_at",
        "source",
        "evidence_scope",
        "aggregate_positive_ev_rows",
        "aggregate_positive_ev_no_executable_book_rows",
        "aggregate_clean_execution_rows",
        "aggregate_risk_ready_rows",
        "aggregate_paper_ready_candidates",
        "primary_gap_after_refresh",
        "best_ev_candidate_ticker",
        "what_would_make_paper_ready",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["what_would_make_paper_ready"] = "; ".join(
                row.get("what_would_make_paper_ready") or []
            )
            writer.writerow({field: payload.get(field, "") for field in fieldnames})


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cents(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return decimal_to_str(value * Decimal("100"))
