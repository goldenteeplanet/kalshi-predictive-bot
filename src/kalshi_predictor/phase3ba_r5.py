from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.schema import (
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    PaperPnl,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ba_r3 import build_phase3ba_r3_weather_paper_gate
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BA_R5_VERSION = "phase3ba_r5_unified_paper_ready_truth_v1"
CRYPTO_R4_PATH = Path("phase3ba_r4/crypto_executable_book_watch.json")
WEATHER_R2_PATH = Path("phase3ba_r2/weather_ranking_activation.json")
WEATHER_R13_PATH = Path("phase3az_r13_weather/weather_handoff_status.json")
R5_STATUS_PATH = Path("phase3bc_r5/phase3bc_r5_status.json")
R13_CLOUD_ADOPTION_PATH = Path("phase3bb_r13/cloud_scheduler_adoption.json")
THREE_AP_GATE_PATH = Path("phase3ap/paper_ready_gate.json")
FRESH_REPORT_SECONDS = 30 * 60

FUNNEL_STEPS = (
    "current active market",
    "verified link",
    "source/snapshot fresh",
    "forecast fresh",
    "ranking fresh",
    "positive EV",
    "executable EV",
    "executable book",
    "liquidity/spread",
    "settlement terms",
    "risk/size approval",
    "paper-ready",
)


@dataclass(frozen=True)
class Phase3BAR5ArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    json_path: Path
    markdown_path: Path
    paper_ready_rows_path: Path
    blocked_rows_path: Path
    reconciliation_sources_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3ba_r5_paper_ready_truth_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r5"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    max_duration_seconds: int = 120,
    limit: int = 500,
) -> Phase3BAR5ArtifactSet:
    payload = build_phase3ba_r5_paper_ready_truth(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        max_duration_seconds=max_duration_seconds,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    json_path = output_dir / "paper_ready_truth.json"
    markdown_path = output_dir / "paper_ready_truth.md"
    paper_ready_rows_path = output_dir / "paper_ready_rows.csv"
    blocked_rows_path = output_dir / "blocked_rows.csv"
    reconciliation_sources_path = output_dir / "reconciliation_sources.json"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_rows_csv(paper_ready_rows_path, payload["paper_ready_rows"])
    _write_rows_csv(blocked_rows_path, payload["blocked_rows"])
    reconciliation_sources_path.write_text(
        json.dumps(payload["reconciliation_sources"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            json_path,
            markdown_path,
            paper_ready_rows_path,
            blocked_rows_path,
            reconciliation_sources_path,
            next_actions_path,
        ],
    )
    return Phase3BAR5ArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        json_path=json_path,
        markdown_path=markdown_path,
        paper_ready_rows_path=paper_ready_rows_path,
        blocked_rows_path=blocked_rows_path,
        reconciliation_sources_path=reconciliation_sources_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3ba_r5_paper_ready_truth(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_r5"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    max_duration_seconds: int = 120,
    limit: int = 500,
) -> dict[str, Any]:
    started = time.monotonic()
    generated_at = utc_now()
    resolved = settings or get_settings()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=generated_at.isoformat(),
        command_args=command_args or [],
    )
    crypto = _crypto_truth_from_r4(reports_dir=reports_dir, now=generated_at)
    weather = _weather_truth_from_reports(reports_dir=reports_dir, now=generated_at)
    rows = [*crypto["rows"], *weather["rows"]]
    paper_ready_rows = [row for row in rows if row["paper_ready"]]
    blocked_rows = [row for row in rows if not row["paper_ready"]]
    category_summaries = {
        "crypto": crypto["summary"],
        "weather": weather["summary"],
        "sports": _diagnostic_only_category("sports"),
        "economic": _diagnostic_only_category("economic"),
        "news": _diagnostic_only_category("news"),
        "general": _diagnostic_only_category("general"),
    }
    freshest_trusted_at = _freshest_generated_at([crypto, weather])
    three_ap = _classify_3ap_gate(
        reports_dir=reports_dir,
        now=generated_at,
        freshest_trusted_at=freshest_trusted_at,
    )
    summary = _summary(rows, category_summaries=category_summaries)
    dashboard_truth = _dashboard_truth(summary, category_summaries=category_summaries)
    duration_seconds = round(time.monotonic() - started, 3)
    status = _status(summary, duration_seconds=duration_seconds, max_duration=max_duration_seconds)
    payload = {
        **metadata,
        "phase": "3BA-R5",
        "phase_version": PHASE3BA_R5_VERSION,
        "mode": "PAPER_READ_ONLY_UNIFIED_PAPER_READY_TRUTH",
        "status": status,
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "parameters": {
            "max_duration_seconds": max_duration_seconds,
            "limit": limit,
            "fresh_report_seconds": FRESH_REPORT_SECONDS,
        },
        "duration_seconds": duration_seconds,
        "bounded_time": {
            "max_duration_seconds": max_duration_seconds,
            "finished_under_bound": duration_seconds <= max_duration_seconds,
        },
        "funnel": list(FUNNEL_STEPS),
        "summary": summary,
        "dashboard_truth": dashboard_truth,
        "category_summaries": category_summaries,
        "trusted_reports": {
            "crypto": crypto["report_status"],
            "weather": weather["report_status"],
            "phase3ap": three_ap,
        },
        "reconciliation_sources": {
            "crypto": crypto.get("reconciliation_sources", {}),
            "weather": weather.get("reconciliation_sources", {}),
            "phase3ap": three_ap,
        },
        "paper_ready_rows": paper_ready_rows,
        "blocked_rows": blocked_rows,
        "optional_diagnostic_categories": {
            "sports": category_summaries["sports"],
            "economic": category_summaries["economic"],
            "news": category_summaries["news"],
            "general": category_summaries["general"],
        },
        "acceptance": _acceptance(
            summary=summary,
            duration_seconds=duration_seconds,
            max_duration_seconds=max_duration_seconds,
            three_ap=three_ap,
        ),
        "next_action": _next_action(summary, category_summaries=category_summaries),
        "operator_guardrails": _operator_guardrails(),
    }
    return payload


def _crypto_truth_from_r4(*, reports_dir: Path, now: Any) -> dict[str, Any]:
    path = reports_dir / CRYPTO_R4_PATH
    payload = _read_json_if_exists(path)
    report_status = _report_status(path=path, payload=payload, now=now)
    r5_path = reports_dir / R5_STATUS_PATH
    r5_payload, r5_selected_path = _freshest_r5_status_payload(reports_dir)
    r5_status = _r5_status_truth(path=r5_selected_path, payload=r5_payload, now=now)
    rows = [
        _crypto_row(row, report_status=report_status)
        for row in payload.get("positive_ev_rows", [])
    ]
    selected_source = "PHASE3BA_R4_DB_ROWS"
    if not rows and _to_int(r5_status.get("positive_ev_rows")) > 0:
        report_status = r5_status["report_status"]
        rows = [_crypto_row(_r5_aggregate_crypto_row(r5_status), report_status=report_status)]
        selected_source = "R5_STATUS_JSON"
    current_rows = sum(_row_weight(row) for row in rows)
    paper_ready_rows = sum(_paper_ready_weight(row) for row in rows)
    blocked_rows = max(0, current_rows - paper_ready_rows)
    positive_ev_rows = sum(_positive_ev_weight(row) for row in rows)
    summary_source = (
        "R5_AGGREGATE_TRUTH_ONLY"
        if selected_source == "R5_STATUS_JSON"
        else report_status["path"]
    )
    summary = {
        "category": "crypto",
        "model_name": "crypto_v2",
        "source": summary_source,
        "status": "R5_AGGREGATE_TRUTH_ONLY"
        if selected_source == "R5_STATUS_JSON"
        else payload.get("status") or "REPORT_MISSING",
        "report_generated_at": report_status.get("generated_at"),
        "report_freshness": report_status["freshness"],
        "current_rows": current_rows,
        "paper_ready_rows": paper_ready_rows,
        "blocked_rows": blocked_rows,
        "positive_ev_rows": positive_ev_rows,
        "first_blocker": _first_blocker(rows),
        "blocker_counts": _weighted_blocker_counts(rows),
        "evidence_scope": "R5_AGGREGATE_TRUTH_ONLY"
        if selected_source == "R5_STATUS_JSON"
        else "ROW_LEVEL_TRUTH",
        "r5_primary_gap_after_refresh": r5_status.get("primary_gap_after_refresh"),
        "r5_watch_state": r5_status.get("watch_state"),
        "r5_aggregate_truth_only": selected_source == "R5_STATUS_JSON",
    }
    reconciliation_sources = {
        "source_precedence": [
            "CURRENT_DB_ROWS",
            "PHASE3BA_R4_REPORT",
            "R5_STATUS_JSON",
            "STALE_REPORTS_DIAGNOSTIC_ONLY",
        ],
        "selected_source": selected_source,
        "r4_path": str(path),
        "r4_loaded": bool(payload),
        "r4_rows": len(payload.get("positive_ev_rows", [])) if payload else 0,
        "r5_path": str(r5_path),
        "r5_selected_path": str(r5_selected_path),
        "r13_cloud_adoption_path": str(reports_dir / R13_CLOUD_ADOPTION_PATH),
        "r5_loaded": bool(r5_payload),
        "r5_positive_ev_rows": r5_status.get("positive_ev_rows"),
        "r5_positive_ev_no_executable_book_rows": r5_status.get(
            "positive_ev_no_executable_book_rows"
        ),
        "r5_paper_ready_candidates": r5_status.get("paper_ready_candidates"),
        "r5_primary_gap_after_refresh": r5_status.get("primary_gap_after_refresh"),
        "aggregate_truth_only": selected_source == "R5_STATUS_JSON",
    }
    return {
        "rows": rows,
        "summary": summary,
        "report_status": report_status,
        "reconciliation_sources": reconciliation_sources,
    }


def _freshest_r5_status_payload(reports_dir: Path) -> tuple[dict[str, Any], Path]:
    local_path = reports_dir / R5_STATUS_PATH
    r13_path = reports_dir / R13_CLOUD_ADOPTION_PATH
    local_payload = _read_json_if_exists(local_path)
    r13_payload = _r13_remote_r5_status(_read_json_if_exists(r13_path))
    payload, path = _freshest_r5_candidate(
        [
            (local_payload, local_path),
            (r13_payload, r13_path),
        ]
    )
    return payload, path


def _r13_remote_r5_status(payload: dict[str, Any]) -> dict[str, Any]:
    parsed = payload.get("parsed_remote_state") if isinstance(payload, dict) else {}
    parsed = parsed if isinstance(parsed, dict) else {}
    guard_dry_run = (
        parsed.get("guard_dry_run") if isinstance(parsed.get("guard_dry_run"), dict) else {}
    )
    guard_after = (
        guard_dry_run.get("after") if isinstance(guard_dry_run.get("after"), dict) else {}
    )
    r5_status = parsed.get("r5_status") if isinstance(parsed.get("r5_status"), dict) else {}
    status, _path = _freshest_r5_candidate(
        [
            (r5_status, Path("phase3bb_r13.parsed_remote_state.r5_status")),
            (guard_after, Path("phase3bb_r13.parsed_remote_state.guard_dry_run.after")),
        ]
    )
    return status


def _freshest_r5_candidate(
    candidates: list[tuple[dict[str, Any], Path]],
) -> tuple[dict[str, Any], Path]:
    best_payload: dict[str, Any] = {}
    best_path = Path("")
    best_generated = None
    for payload, path in candidates:
        if not payload:
            continue
        generated = parse_datetime(payload.get("generated_at"))
        if best_payload and generated is not None and best_generated is not None:
            if generated <= best_generated:
                continue
        elif best_payload and generated is None:
            continue
        best_payload = payload
        best_path = path
        best_generated = generated
    return best_payload, best_path


def _crypto_row(row: dict[str, Any], *, report_status: dict[str, Any]) -> dict[str, Any]:
    watch_state = str(row.get("watch_state") or "UNKNOWN")
    blocker = _crypto_blocker(row)
    paper_ready = watch_state == "PAPER_READY"
    return {
        "category": "crypto",
        "model_name": "crypto_v2",
        "ticker": row.get("ticker"),
        "market": row.get("clean_title"),
        "paper_ready": paper_ready,
        "first_blocker": "PAPER_READY" if paper_ready else blocker,
        "specific_blocker": row.get("execution_blocker_detail"),
        "current_active_market": bool(row.get("current_market") and row.get("active_market")),
        "verified_link": bool(
            row.get("exact_catalog_or_verified_link") or row.get("verified_kalshi_url")
        ),
        "source_snapshot_fresh": bool(row.get("snapshot_present")),
        "forecast_fresh": bool(row.get("latest_forecast_at")),
        "ranking_fresh": bool(row.get("latest_ranking_at")),
        "positive_ev": True,
        "executable_ev": row.get("watch_state") not in {"POSITIVE_EV_NO_BOOK"},
        "executable_book": bool(row.get("book_usable")),
        "liquidity_spread_pass": bool(row.get("liquidity_pass") and row.get("spread_pass")),
        "settlement_terms": None,
        "risk_size_approval": bool(row.get("phase3m_nonzero_size") and row.get("phase3n_approved")),
        "score": row.get("opportunity_score"),
        "expected_value": row.get("expected_value"),
        "expected_value_cents": row.get("expected_value_cents"),
        "watch_state": watch_state,
        "report_freshness": report_status["freshness"],
        "source_report": report_status["path"],
        "generated_at": report_status.get("generated_at"),
        "source": row.get("source"),
        "evidence_scope": row.get("evidence_scope"),
        "row_weight": _row_weight(row),
        "aggregate_positive_ev_rows": row.get("aggregate_positive_ev_rows"),
        "aggregate_positive_ev_no_executable_book_rows": row.get(
            "aggregate_positive_ev_no_executable_book_rows"
        ),
        "primary_gap_after_refresh": row.get("primary_gap_after_refresh"),
        "best_ev_candidate_ticker": row.get("best_ev_candidate_ticker"),
        "recommended_action": "; ".join(row.get("what_would_make_paper_ready") or []),
    }


def _crypto_blocker(row: dict[str, Any]) -> str:
    state = str(row.get("watch_state") or "")
    detail = str(row.get("execution_blocker_detail") or "")
    if state == "POSITIVE_EV_NO_BOOK":
        if detail == "POSITIVE_EV_NO_EXECUTABLE_BOOK":
            return "POSITIVE_EV_NO_EXECUTABLE_BOOK"
        return "EXECUTABLE_BOOK_MISSING" if detail != "ZERO_VISIBLE_DEPTH" else "ZERO_VISIBLE_DEPTH"
    if state == "POSITIVE_EV_THIN_BOOK":
        return "LIQUIDITY_TOO_LOW"
    if state == "POSITIVE_EV_WIDE_SPREAD":
        return "SPREAD_TOO_WIDE"
    if state == "POSITIVE_EV_READY_FOR_RISK":
        return "RISK_SIZE_PENDING"
    if state == "POSITIVE_EV_RISK_NOT_ELIGIBLE":
        return detail or "RISK_NOT_ELIGIBLE"
    return state or "UNKNOWN"


def _r5_status_truth(*, path: Path, payload: dict[str, Any], now: Any) -> dict[str, Any]:
    latest_summary = payload.get("latest_summary") or {}
    guard = payload.get("guard") or {}
    status_summary = payload.get("summary") or {}
    report_status = _report_status(path=path, payload=payload, now=now)
    return {
        "report_status": report_status,
        "latest_summary": latest_summary,
        "guard": guard,
        "history_rows": payload.get("history_rows"),
        "watch_state": _first_present(
            payload.get("latest_watch_state"),
            latest_summary.get("watch_state"),
            guard.get("watch_state"),
            status_summary.get("watch_state"),
        ),
        "paper_ready_candidates": _first_present(
            latest_summary.get("paper_ready_candidates"),
            guard.get("paper_ready_candidates"),
            status_summary.get("paper_ready_candidates"),
        ),
        "positive_ev_rows": _first_present(
            latest_summary.get("positive_ev_rows"),
            guard.get("positive_ev_rows"),
            status_summary.get("positive_ev_rows"),
        ),
        "positive_ev_no_executable_book_rows": _first_present(
            latest_summary.get("positive_ev_no_executable_book_rows"),
            guard.get("positive_ev_no_executable_book_rows"),
            latest_summary.get("positive_ev_no_book_rows"),
            status_summary.get("positive_ev_no_executable_book_rows"),
        ),
        "clean_execution_rows": _first_present(
            latest_summary.get("clean_execution_rows"),
            guard.get("clean_execution_rows"),
            status_summary.get("clean_execution_rows"),
        ),
        "risk_ready_rows": _first_present(
            latest_summary.get("risk_ready_rows"),
            guard.get("risk_ready_rows"),
            status_summary.get("risk_ready_rows"),
        ),
        "primary_gap_after_refresh": _first_present(
            latest_summary.get("primary_gap_after_refresh"),
            guard.get("primary_gap_after_refresh"),
            status_summary.get("primary_gap_after_refresh"),
        ),
        "best_ev_candidate_ticker": _first_present(
            latest_summary.get("best_ev_candidate_ticker"),
            guard.get("best_ev_candidate_ticker"),
            status_summary.get("best_ev_candidate_ticker"),
        ),
        "best_current_expected_value_cents": _first_present(
            latest_summary.get("best_current_expected_value_cents"),
            latest_summary.get("best_ev_cents"),
            guard.get("best_current_expected_value_cents"),
            status_summary.get("best_current_expected_value_cents"),
        ),
        "loaded": bool(payload),
    }


def _r5_aggregate_crypto_row(r5_status: dict[str, Any]) -> dict[str, Any]:
    positive_ev_rows = _to_int(r5_status.get("positive_ev_rows"))
    no_book_rows = _to_int(r5_status.get("positive_ev_no_executable_book_rows"))
    clean_execution_rows = _to_int(r5_status.get("clean_execution_rows"))
    primary_gap = r5_status.get("primary_gap_after_refresh")
    watch_state = "POSITIVE_EV_NO_BOOK" if no_book_rows > 0 else "POSITIVE_EV_RISK_NOT_ELIGIBLE"
    detail = "POSITIVE_EV_NO_EXECUTABLE_BOOK" if no_book_rows > 0 else str(
        primary_gap or "R5_AGGREGATE_POSITIVE_EV_BLOCKED"
    )
    return {
        "ticker": None,
        "clean_title": "R5 aggregate crypto truth; row-level opportunity detail unavailable",
        "current_market": True,
        "active_market": True,
        "verified_kalshi_url": None,
        "exact_catalog_or_verified_link": None,
        "snapshot_present": None,
        "latest_forecast_at": None,
        "latest_ranking_at": r5_status.get("report_status", {}).get("generated_at"),
        "book_usable": clean_execution_rows > 0 and no_book_rows <= 0,
        "liquidity_pass": clean_execution_rows > 0,
        "spread_pass": clean_execution_rows > 0,
        "phase3m_nonzero_size": False,
        "phase3n_approved": False,
        "watch_state": watch_state,
        "execution_blocker_detail": detail,
        "expected_value_cents": r5_status.get("best_current_expected_value_cents"),
        "source": "R5_STATUS_JSON",
        "evidence_scope": "R5_AGGREGATE_TRUTH_ONLY",
        "aggregate_positive_ev_rows": positive_ev_rows,
        "aggregate_positive_ev_no_executable_book_rows": no_book_rows,
        "aggregate_clean_execution_rows": clean_execution_rows,
        "aggregate_risk_ready_rows": _to_int(r5_status.get("risk_ready_rows")),
        "aggregate_paper_ready_candidates": _to_int(r5_status.get("paper_ready_candidates")),
        "primary_gap_after_refresh": primary_gap,
        "best_ev_candidate_ticker": r5_status.get("best_ev_candidate_ticker"),
        "what_would_make_paper_ready": [
            "R5 status JSON has aggregate positive-EV crypto truth; materialized row details "
            "are unavailable because opportunity diagnostics were skipped or not persisted."
        ],
    }


def _row_weight(row: dict[str, Any]) -> int:
    if row.get("evidence_scope") == "R5_AGGREGATE_TRUTH_ONLY":
        return max(0, _to_int(row.get("aggregate_positive_ev_rows")))
    return 1


def _paper_ready_weight(row: dict[str, Any]) -> int:
    if row.get("evidence_scope") == "R5_AGGREGATE_TRUTH_ONLY":
        return max(0, _to_int(row.get("aggregate_paper_ready_candidates")))
    return int(bool(row.get("paper_ready")) or row.get("watch_state") == "PAPER_READY")


def _positive_ev_weight(row: dict[str, Any]) -> int:
    if row.get("evidence_scope") == "R5_AGGREGATE_TRUTH_ONLY":
        return max(0, _to_int(row.get("aggregate_positive_ev_rows")))
    return int(bool(row.get("positive_ev", True)))


def _weighted_blocker_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter = Counter()
    for row in rows:
        if not row.get("paper_ready"):
            counts[str(row.get("first_blocker") or "UNKNOWN")] += _row_weight(row)
    return dict(counts)


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


def _weather_truth_from_reports(*, reports_dir: Path, now: Any) -> dict[str, Any]:
    r2_path = reports_dir / WEATHER_R2_PATH
    r13_path = reports_dir / WEATHER_R13_PATH
    r2_payload = _read_json_if_exists(r2_path)
    r13_payload = _read_json_if_exists(r13_path)
    if not r2_payload and not r13_payload:
        report_status = {
            "path": f"{r2_path}; {r13_path}",
            "loaded": False,
            "generated_at": None,
            "age_seconds": None,
            "freshness": "WEATHER_TRUTH_MISSING",
        }
        summary = {
            "category": "weather",
            "model_name": "weather_v2",
            "source": "weather_truth_reports_missing",
            "status": "WEATHER_TRUTH_MISSING",
            "report_generated_at": None,
            "report_freshness": "WEATHER_TRUTH_MISSING",
            "current_rows": 0,
            "paper_ready_rows": 0,
            "blocked_rows": 0,
            "positive_ev_rows": 0,
            "first_blocker": "WEATHER_TRUTH_MISSING",
            "blocker_counts": {
                "WEATHER_TRUTH_MISSING": 1,
                "WEATHER_RANKING_REPORT_MISSING": 1,
                "WEATHER_PAPER_GATE_REPORT_MISSING": 1,
            },
            "input_reports": {
                "r2_loaded": False,
                "r2_path": str(r2_path),
                "r13_loaded": False,
                "r13_path": str(r13_path),
                "selected": None,
            },
        }
        return {
            "rows": [],
            "summary": summary,
            "report_status": report_status,
            "reconciliation_sources": {
                "selected_source": "WEATHER_TRUTH_MISSING",
                "r2_path": str(r2_path),
                "r2_loaded": False,
                "r13_path": str(r13_path),
                "r13_loaded": False,
                "missing_blockers": [
                    "WEATHER_TRUTH_MISSING",
                    "WEATHER_RANKING_REPORT_MISSING",
                    "WEATHER_PAPER_GATE_REPORT_MISSING",
                ],
            },
        }
    r2_generated = parse_datetime(r2_payload.get("generated_at")) if r2_payload else None
    r13_generated = parse_datetime(r13_payload.get("generated_at")) if r13_payload else None
    use_r2 = bool(
        r2_payload.get("weather_rows")
        and (r13_generated is None or (r2_generated is not None and r2_generated >= r13_generated))
    )
    if use_r2:
        path = r2_path
        payload = r2_payload
        source = "phase3ba_r2_weather_ranking_activation"
        raw_rows = payload.get("weather_rows", [])
    else:
        path = r13_path
        payload = r13_payload
        source = "phase3az_r13_weather_handoff"
        raw_rows = payload.get("handoff_rows", [])
    report_status = _report_status(path=path, payload=payload, now=now)
    rows = [_weather_row(row, report_status=report_status) for row in raw_rows]
    summary = {
        "category": "weather",
        "model_name": "weather_v2",
        "source": source,
        "status": payload.get("status") or source,
        "report_generated_at": payload.get("generated_at"),
        "report_freshness": report_status["freshness"],
        "current_rows": len(rows),
        "paper_ready_rows": sum(1 for row in rows if row["paper_ready"]),
        "blocked_rows": sum(1 for row in rows if not row["paper_ready"]),
        "positive_ev_rows": sum(1 for row in rows if row["positive_ev"]),
        "first_blocker": _first_blocker(rows),
        "blocker_counts": dict(Counter(row["first_blocker"] for row in rows)),
        "input_reports": {
            "r2_loaded": bool(r2_payload),
            "r2_generated_at": r2_payload.get("generated_at"),
            "r13_loaded": bool(r13_payload),
            "r13_generated_at": r13_payload.get("generated_at"),
            "selected": str(path),
        },
    }
    return {
        "rows": rows,
        "summary": summary,
        "report_status": report_status,
        "reconciliation_sources": {
            "selected_source": str(path),
            "r2_path": str(r2_path),
            "r2_loaded": bool(r2_payload),
            "r2_generated_at": r2_payload.get("generated_at"),
            "r13_path": str(r13_path),
            "r13_loaded": bool(r13_payload),
            "r13_generated_at": r13_payload.get("generated_at"),
        },
    }


def _weather_truth(
    session: Session,
    *,
    output_dir: Path,
    reports_dir: Path,
    settings: Settings,
    started: float,
    max_duration_seconds: int,
    limit: int,
) -> dict[str, Any]:
    if _remaining_seconds(started, max_duration_seconds) < 10:
        status = {
            "path": "built_in_process",
            "freshness": "SKIPPED_TIME_BUDGET",
            "generated_at": None,
        }
        return {
            "rows": [],
            "summary": _empty_category_summary("weather", "weather_v2", status),
            "report_status": status,
        }
    try:
        payload = build_phase3ba_r3_weather_paper_gate(
            session,
            output_dir=output_dir,
            reports_dir=reports_dir,
            settings=settings,
            command_args=["phase3ba-r5-paper-ready-truth", "embedded_weather_gate"],
            limit=limit,
        )
    except Exception as exc:
        status = {
            "path": "embedded_phase3ba_r3_weather_gate",
            "freshness": "WEATHER_GATE_ERROR",
            "generated_at": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
        summary = _empty_category_summary("weather", "weather_v2", status)
        summary["status"] = "WEATHER_GATE_ERROR"
        summary["first_blocker"] = "WEATHER_GATE_ERROR"
        summary["blocker_counts"] = {"WEATHER_GATE_ERROR": 1}
        return {"rows": [], "summary": summary, "report_status": status}
    status = {
        "path": "embedded_phase3ba_r3_weather_gate",
        "freshness": "CURRENT_EMBEDDED",
        "generated_at": payload.get("generated_at"),
    }
    rows = [_weather_row(row, report_status=status) for row in payload.get("weather_rows", [])]
    summary = {
        "category": "weather",
        "model_name": "weather_v2",
        "source": "embedded_phase3ba_r3_weather_gate",
        "status": payload.get("status"),
        "report_generated_at": payload.get("generated_at"),
        "report_freshness": status["freshness"],
        "current_rows": len(rows),
        "paper_ready_rows": sum(1 for row in rows if row["paper_ready"]),
        "blocked_rows": sum(1 for row in rows if not row["paper_ready"]),
        "positive_ev_rows": sum(1 for row in rows if row["positive_ev"]),
        "first_blocker": _first_blocker(rows),
        "blocker_counts": dict(Counter(row["first_blocker"] for row in rows)),
    }
    return {"rows": rows, "summary": summary, "report_status": status}


def _weather_row(row: dict[str, Any], *, report_status: dict[str, Any]) -> dict[str, Any]:
    blocker = str(
        row.get("first_blocker")
        or row.get("first_hard_blocker")
        or _weather_handoff_blocker(row)
        or "UNKNOWN"
    )
    paper_ready = bool(row.get("paper_ready")) or blocker == "PAPER_READY"
    linked = bool(
        row.get("verified_kalshi_url") or row.get("link_detected_at") or row.get("ticker")
    )
    snapshot_fresh = bool(row.get("snapshot_fresh") or row.get("has_snapshot"))
    forecast_fresh = bool(row.get("has_current_forecast"))
    ranking_fresh = bool(row.get("has_current_ranking"))
    active_market = str(row.get("market_status") or "").lower() in {"active", "open"}
    return {
        "category": "weather",
        "model_name": "weather_v2",
        "ticker": row.get("ticker"),
        "market": row.get("market_title") or row.get("clean_title"),
        "paper_ready": paper_ready,
        "first_blocker": blocker,
        "specific_blocker": blocker,
        "current_active_market": bool(row.get("current_window_eligible") or active_market),
        "verified_link": linked,
        "source_snapshot_fresh": snapshot_fresh,
        "forecast_fresh": forecast_fresh,
        "ranking_fresh": ranking_fresh,
        "positive_ev": blocker
        not in {
            "SOURCE_MISSING",
            "SNAPSHOT_MISSING",
            "SNAPSHOT_STALE",
            "FORECAST_MISSING",
            "RANKING_MISSING",
            "EV_NOT_POSITIVE",
        },
        "executable_ev": blocker not in {"EXECUTABLE_EV_NOT_POSITIVE", "EV_NOT_POSITIVE"},
        "executable_book": bool(row.get("executable_book") or row.get("best_side")),
        "liquidity_spread_pass": bool(
            blocker not in {"BOOK_MISSING", "LIQUIDITY_TOO_LOW", "SPREAD_TOO_WIDE"}
            and row.get("best_side")
        ),
        "settlement_terms": bool(row.get("settlement_terms_known")),
        "risk_size_approval": bool(row.get("phase3m_nonzero_size") and row.get("phase3n_approved")),
        "score": row.get("opportunity_score"),
        "expected_value": row.get("raw_ev") or row.get("estimated_edge"),
        "expected_value_cents": None,
        "watch_state": None,
        "report_freshness": report_status["freshness"],
        "source_report": report_status["path"],
        "generated_at": report_status.get("generated_at"),
        "recommended_action": row.get("what_would_make_paper_ready") or blocker,
    }


def _weather_handoff_blocker(row: dict[str, Any]) -> str:
    if not row.get("has_snapshot"):
        return "SNAPSHOT_MISSING"
    if not row.get("has_current_forecast"):
        return "FORECAST_MISSING"
    if not row.get("has_current_ranking"):
        return "RANKING_MISSING"
    return "RANKING_AVAILABLE_NEEDS_PAPER_GATE"


def _classify_3ap_gate(
    *,
    reports_dir: Path,
    now: Any,
    freshest_trusted_at: Any | None,
) -> dict[str, Any]:
    path = reports_dir / THREE_AP_GATE_PATH
    payload = _read_json_if_exists(path)
    generated_at = parse_datetime(payload.get("generated_at")) if payload else None
    age_seconds = _age_seconds(generated_at, now) if generated_at is not None else None
    older_than_trusted = bool(
        generated_at is not None
        and freshest_trusted_at is not None
        and generated_at < freshest_trusted_at
    )
    stale = not payload or older_than_trusted or (
        age_seconds is not None and age_seconds > FRESH_REPORT_SECONDS
    )
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    return {
        "path": str(path),
        "loaded": bool(payload),
        "generated_at": generated_at.isoformat() if generated_at else None,
        "age_seconds": age_seconds,
        "freshness": "HISTORICAL_STALE" if stale else "CURRENT",
        "older_than_freshest_trusted_report": older_than_trusted,
        "stale_artifact_not_used_as_current_truth": stale,
        "summary": {
            "paper_ready_rows": summary.get("paper_ready_rows"),
            "positive_ev_rows": summary.get("positive_ev_rows"),
            "first_hard_blocker": summary.get("first_hard_blocker"),
        },
    }


def _report_status(*, path: Path, payload: dict[str, Any], now: Any) -> dict[str, Any]:
    generated_at = parse_datetime(payload.get("generated_at")) if payload else None
    age_seconds = _age_seconds(generated_at, now) if generated_at is not None else None
    if not payload:
        freshness = "MISSING"
    elif age_seconds is not None and age_seconds <= FRESH_REPORT_SECONDS:
        freshness = "CURRENT"
    else:
        freshness = "STALE"
    return {
        "path": str(path),
        "loaded": bool(payload),
        "generated_at": generated_at.isoformat() if generated_at else None,
        "age_seconds": age_seconds,
        "freshness": freshness,
    }


def _summary(
    rows: list[dict[str, Any]],
    *,
    category_summaries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    blockers: Counter = Counter()
    for row in rows:
        if not row["paper_ready"]:
            blockers[str(row["first_blocker"])] += _row_weight(row)
    if not blockers:
        for category in ("crypto", "weather"):
            category_summary = category_summaries.get(category, {})
            blocker = str(category_summary.get("first_blocker") or "")
            if blocker and blocker != "PAPER_READY":
                blockers[blocker] += max(
                    1,
                    int(category_summary.get("blocked_rows") or 0),
                    int(category_summary.get("current_rows") or 0),
                )
    paper_ready_rows = sum(_paper_ready_weight(row) for row in rows)
    weighted_rows = sum(_row_weight(row) for row in rows)
    first_hard_blocker = _summary_first_hard_blocker(
        blockers,
        category_summaries=category_summaries,
    )
    return {
        "rows_scanned": weighted_rows,
        "paper_ready_rows": paper_ready_rows,
        "blocked_rows": max(0, weighted_rows - paper_ready_rows),
        "current_categories": {
            category: summary.get("current_rows", 0)
            for category, summary in category_summaries.items()
        },
        "paper_ready_by_category": {
            category: summary.get("paper_ready_rows", 0)
            for category, summary in category_summaries.items()
        },
        "blocked_by_category": {
            category: summary.get("blocked_rows", 0)
            for category, summary in category_summaries.items()
        },
        "positive_ev_rows": sum(_positive_ev_weight(row) for row in rows),
        "first_hard_blocker": first_hard_blocker,
        "blocker_counts": dict(blockers),
        "dashboard_truth_source": "phase3ba_r5_current_unified_truth",
    }


def _summary_first_hard_blocker(
    blockers: Counter,
    *,
    category_summaries: dict[str, dict[str, Any]],
) -> str:
    crypto = category_summaries.get("crypto", {})
    crypto_positive_ev_rows = _to_int(crypto.get("positive_ev_rows"))
    crypto_paper_ready_rows = _to_int(crypto.get("paper_ready_rows"))
    crypto_blocker = str(crypto.get("first_blocker") or "")
    if (
        crypto_positive_ev_rows > 0
        and crypto_paper_ready_rows == 0
        and crypto_blocker
        and crypto_blocker not in {"PAPER_READY", "NO_CURRENT_ROWS"}
    ):
        return crypto_blocker
    return blockers.most_common(1)[0][0] if blockers else "PAPER_READY"


def _dashboard_truth(
    summary: dict[str, Any],
    *,
    category_summaries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ready = int(summary["paper_ready_rows"])
    blocked = int(summary["blocked_rows"])
    if ready > 0:
        status_kind = "ready"
        status_label = "Paper Ready Review"
    elif blocked > 0:
        status_kind = "blocked"
        status_label = "Paper Gate Blocked"
    else:
        status_kind = "watching"
        status_label = "Waiting For Current Candidates"
    return {
        "summary": (
            f"{ready} paper-ready row(s), {blocked} blocked current row(s), "
            f"first blocker {summary['first_hard_blocker']}."
        ),
        "status_kind": status_kind,
        "status_label": status_label,
        "metrics": {
            "paper_ready_rows": ready,
            "blocked_rows": blocked,
            "positive_ev_rows": summary["positive_ev_rows"],
            "crypto_rows": category_summaries["crypto"].get("current_rows", 0),
            "weather_rows": category_summaries["weather"].get("current_rows", 0),
        },
        "last_updated": utc_now().isoformat(),
        "blockers": summary["blocker_counts"],
        "report_links": {
            "unified_truth": "reports/phase3ba_r5/paper_ready_truth.json",
            "crypto": "reports/phase3ba_r4/crypto_executable_book_watch.json",
            "phase3ap": "reports/phase3ap/paper_ready_gate.json",
        },
    }


def _status(summary: dict[str, Any], *, duration_seconds: float, max_duration: int) -> str:
    if duration_seconds > max_duration:
        return "BOUNDED_REPORT_TIMEOUT_RISK"
    if summary["paper_ready_rows"] > 0:
        return "PAPER_READY_ROWS_PRESENT"
    if summary["blocked_rows"] > 0:
        return "PAPER_READY_TRUTH_BLOCKED"
    return "NO_CURRENT_PAPER_READY_ROWS"


def _acceptance(
    *,
    summary: dict[str, Any],
    duration_seconds: float,
    max_duration_seconds: int,
    three_ap: dict[str, Any],
) -> dict[str, Any]:
    return {
        "report_finishes_under_bounded_time": duration_seconds <= max_duration_seconds,
        "crypto_and_weather_current_blockers_shown": True,
        "phase3ap_stale_classified_historical_if_applicable": (
            three_ap["freshness"] in {"CURRENT", "HISTORICAL_STALE"}
        ),
        "dashboard_can_consume_truth": True,
        "no_paper_trades_created": True,
        "no_live_or_demo_orders": True,
        "paper_ready_rows": summary["paper_ready_rows"],
    }


def _next_action(
    summary: dict[str, Any],
    *,
    category_summaries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if summary["paper_ready_rows"] > 0:
        return {
            "stage": "PAPER_ONLY_OPERATOR_REVIEW",
            "command": (
                "kalshi-bot phase3ba-r5-paper-ready-truth --output-dir "
                "reports/phase3ba_r5 --reports-dir reports --max-duration-seconds 120"
            ),
            "reason": "Paper-ready rows exist; operator review only, no trade creation.",
            "allow_paper_trade_creation": False,
        }
    crypto_blocker = category_summaries["crypto"].get("first_blocker")
    weather_blocker = category_summaries["weather"].get("first_blocker")
    weather_blockers = category_summaries["weather"].get("blocker_counts", {})
    if int(weather_blockers.get("SNAPSHOT_MISSING", 0) or 0) > 0:
        return {
            "stage": "REFRESH_WEATHER_SNAPSHOTS_THEN_REBUILD_TRUTH",
            "command": (
                "kalshi-bot db-writer-monitor --json\n"
                "kalshi-bot snapshot --status open --limit 100 --max-pages 3 "
                "--series-ticker KXTEMPNYCH --include-orderbook\n"
                "kalshi-bot phase3ba-r2-weather-ranking-activation --output-dir "
                "reports/phase3ba_r2 --reports-dir reports --limit 100\n"
                "kalshi-bot phase3ba-r5-paper-ready-truth --output-dir "
                "reports/phase3ba_r5 --reports-dir reports --max-duration-seconds 120"
            ),
            "reason": (
                "Weather has current linked rows missing snapshots; refresh targeted NY "
                "weather orderbooks only after db-writer-monitor is clear, then rebuild rankings "
                "and unified truth."
            ),
            "allow_paper_trade_creation": False,
            "requires_writer_gate_clear": True,
        }
    return {
        "stage": "KEEP_CURRENT_WATCHERS_AND_REFRESH_TRUTH",
        "command": (
            "kalshi-bot phase3ba-r5-paper-ready-truth --output-dir "
            "reports/phase3ba_r5 --reports-dir reports --max-duration-seconds 120"
        ),
        "reason": f"Crypto blocker={crypto_blocker}; weather blocker={weather_blocker}.",
        "allow_paper_trade_creation": False,
    }


def _operator_guardrails() -> list[str]:
    return [
        "Keep PAPER / READ-ONLY.",
        "Do not create paper trades.",
        "Do not submit, cancel, replace, or amend live/demo exchange orders.",
        "Do not lower thresholds.",
        "Exclude expired, historical, and diagnostic-only rows from current truth.",
        "Use current DB state and freshest trusted reports.",
    ]


def _diagnostic_only_category(category: str) -> dict[str, Any]:
    return {
        "category": category,
        "model_name": None,
        "source": "diagnostic_only_not_current_paper_funnel",
        "status": "DIAGNOSTIC_ONLY_DEFERRED",
        "report_generated_at": None,
        "report_freshness": "NOT_PART_OF_CURRENT_R5_FUNNEL",
        "current_rows": 0,
        "paper_ready_rows": 0,
        "blocked_rows": 0,
        "positive_ev_rows": 0,
        "first_blocker": "CATEGORY_NOT_ACTIVATED_FOR_CURRENT_PAPER_GATE",
        "blocker_counts": {},
    }


def _empty_category_summary(
    category: str,
    model_name: str,
    report_status: dict[str, Any],
) -> dict[str, Any]:
    return {
        "category": category,
        "model_name": model_name,
        "source": report_status["path"],
        "status": report_status["freshness"],
        "report_generated_at": report_status.get("generated_at"),
        "report_freshness": report_status["freshness"],
        "current_rows": 0,
        "paper_ready_rows": 0,
        "blocked_rows": 0,
        "positive_ev_rows": 0,
        "first_blocker": report_status["freshness"],
        "blocker_counts": {},
    }


def _first_blocker(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        if not row["paper_ready"]:
            return str(row["first_blocker"])
    return "PAPER_READY" if rows else "NO_CURRENT_ROWS"


def _freshest_generated_at(groups: list[dict[str, Any]]) -> Any | None:
    times = []
    for group in groups:
        generated = group.get("report_status", {}).get("generated_at")
        parsed = parse_datetime(generated)
        if parsed is not None:
            times.append(parsed)
    return max(times) if times else None


def _age_seconds(value: Any | None, now: Any) -> int | None:
    if value is None:
        return None
    return int(max(0, (now - value).total_seconds()))


def _remaining_seconds(started: float, max_duration_seconds: int) -> float:
    return max(0.0, float(max_duration_seconds) - (time.monotonic() - started))


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
            "command": "kalshi-bot phase3ba-r5-paper-ready-truth",
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
        "latest_crypto_v2_ranking_at": _latest_ranking_iso(session, "crypto_v2"),
        "latest_weather_v2_ranking_at": _latest_ranking_iso(session, "weather_v2"),
        "latest_crypto_v2_forecast_at": _latest_forecast_iso(session, "crypto_v2"),
        "latest_weather_v2_forecast_at": _latest_forecast_iso(session, "weather_v2"),
        "latest_paper_order_at": _latest_iso(session, PaperOrder.created_at),
        "latest_paper_pnl_at": _latest_iso(session, PaperPnl.calculated_at),
    }


def _latest_iso(session: Session, column: Any) -> str | None:
    value = session.scalar(select(func.max(column)))
    return value.isoformat() if hasattr(value, "isoformat") else value


def _latest_ranking_iso(session: Session, model_name: str) -> str | None:
    value = session.scalar(
        select(func.max(MarketRanking.ranked_at)).where(
            MarketRanking.forecast_model == model_name
        )
    )
    return value.isoformat() if hasattr(value, "isoformat") else value


def _latest_forecast_iso(session: Session, model_name: str) -> str | None:
    value = session.scalar(
        select(func.max(Forecast.forecasted_at)).where(Forecast.model_name == model_name)
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
        "uses_stale_3ap_as_current_truth": False,
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R5 Unified Paper-Ready Truth")
    summary = payload["summary"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{payload['status']}`",
            f"- Duration seconds: `{payload['duration_seconds']}`",
            f"- Paper-ready rows: `{summary['paper_ready_rows']}`",
            f"- Blocked rows: `{summary['blocked_rows']}`",
            f"- Positive-EV rows: `{summary['positive_ev_rows']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            f"- 3AP freshness: `{payload['trusted_reports']['phase3ap']['freshness']}`",
            "",
            "## Category Snapshot",
            "",
        ]
    )
    for category, category_summary in payload["category_summaries"].items():
        lines.append(
            "- "
            f"{category}: rows={category_summary.get('current_rows')}, "
            f"ready={category_summary.get('paper_ready_rows')}, "
            f"blocked={category_summary.get('blocked_rows')}, "
            f"first={category_summary.get('first_blocker')}"
        )
    lines.extend(
        [
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


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R5 Paper-Ready Truth Detail")
    lines.extend(["", "## Funnel", ""])
    for index, step in enumerate(payload["funnel"], start=1):
        lines.append(f"{index}. {step}")
    lines.extend(["", "## Summary", ""])
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Current Rows",
            "",
            "| Category | Ticker | Ready | First Blocker | Specific | Source |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    rows = [*payload["paper_ready_rows"], *payload["blocked_rows"]]
    if not rows:
        lines.append("| none |  | False | NO_CURRENT_ROWS |  |  |")
    for row in rows:
        lines.append(
            "| "
            f"{row['category']} | "
            f"{row.get('ticker') or ''} | "
            f"{row['paper_ready']} | "
            f"{row['first_blocker']} | "
            f"{row.get('specific_blocker') or ''} | "
            f"{row.get('source_report') or ''} |"
        )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    action = payload["next_action"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R5 Next Actions")
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
        "category",
        "model_name",
        "ticker",
        "market",
        "paper_ready",
        "first_blocker",
        "specific_blocker",
        "current_active_market",
        "verified_link",
        "source_snapshot_fresh",
        "forecast_fresh",
        "ranking_fresh",
        "positive_ev",
        "executable_ev",
        "executable_book",
        "liquidity_spread_pass",
        "settlement_terms",
        "risk_size_approval",
        "score",
        "expected_value",
        "expected_value_cents",
        "source_report",
        "report_freshness",
        "source",
        "evidence_scope",
        "row_weight",
        "aggregate_positive_ev_rows",
        "aggregate_positive_ev_no_executable_book_rows",
        "primary_gap_after_refresh",
        "best_ev_candidate_ticker",
        "recommended_action",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
