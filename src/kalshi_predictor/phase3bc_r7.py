from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.ticker_windows import crypto_ticker_close_time_utc
from kalshi_predictor.data.schema import Forecast, MarketSnapshot
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.opportunities.scanner import build_market_ranking
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3bc import build_phase3bc_crypto_clean_opportunity_router
from kalshi_predictor.phase3bc_r5_alignment import (
    R5_PRIMARY_EV_NOT_POSITIVE,
    apply_r5_truth_to_blocker_summary,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BC_R7_VERSION = "phase3bc_r7_crypto_ranking_coverage_repair"
MODEL_NAME = "crypto_v2"

ISSUE_RANKING_MISSING = "RANKING_MISSING"
ISSUE_RANKING_STALE = "RANKING_STALE"
ISSUE_RANKING_BEFORE_FORECAST = "RANKING_BEFORE_FORECAST"
ISSUE_RANKING_FRESH = "RANKING_FRESH"
ISSUE_EXPIRED_CRYPTO_WINDOW = "EXPIRED_CRYPTO_WINDOW"
REPAIRABLE_ISSUES = {
    ISSUE_RANKING_MISSING,
    ISSUE_RANKING_STALE,
    ISSUE_RANKING_BEFORE_FORECAST,
}


@dataclass(frozen=True)
class Phase3BCR7ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def write_phase3bc_r7_crypto_ranking_coverage_repair_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bc_r7"),
    settings: Settings | None = None,
    limit: int = 2000,
    freshness_minutes: int = 15,
    repair_rankings: bool = False,
    repair_limit: int = 250,
) -> Phase3BCR7ArtifactSet:
    """Diagnose and optionally repair active pure crypto ranking coverage.

    This pass writes MarketRanking rows only for exact active pure crypto rows that
    already have a fresh snapshot and fresh crypto_v2 forecast. It does not create
    orders, paper fills, settlement rows, or opportunity approvals.
    """

    resolved = settings or get_settings()
    before_phase3bc = build_phase3bc_crypto_clean_opportunity_router(
        session,
        settings=resolved,
        limit=limit,
    )
    before_rows = list(before_phase3bc.get("rows", []))
    before_diagnostics = classify_phase3bc_r7_rows(
        before_rows,
        freshness_minutes=freshness_minutes,
    )

    repair_results: list[dict[str, Any]] = []
    if repair_rankings:
        repair_results = _repair_rankings(
            session,
            before_diagnostics,
            settings=resolved,
            repair_limit=repair_limit,
        )

    after_phase3bc: dict[str, Any] | None = None
    after_diagnostics: list[dict[str, Any]] | None = None
    if repair_rankings:
        after_phase3bc = build_phase3bc_crypto_clean_opportunity_router(
            session,
            settings=resolved,
            limit=limit,
        )
        after_diagnostics = classify_phase3bc_r7_rows(
            list(after_phase3bc.get("rows", [])),
            freshness_minutes=freshness_minutes,
        )

    payload = build_phase3bc_r7_payload(
        before_rows=before_rows,
        before_diagnostics=before_diagnostics,
        before_phase3bc_summary=before_phase3bc.get("summary", {}),
        repair_results=repair_results,
        after_diagnostics=after_diagnostics,
        after_phase3bc_summary=(after_phase3bc or {}).get("summary", {}),
        freshness_minutes=freshness_minutes,
        repair_rankings=repair_rankings,
        repair_limit=repair_limit,
        limit=limit,
    )
    _apply_r5_truth_alignment(payload, reports_dir=output_dir.parent)

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bc_r7_crypto_ranking_coverage_repair.json"
    markdown_path = output_dir / "phase3bc_r7_crypto_ranking_coverage_repair.md"
    rows_path = output_dir / "phase3bc_r7_crypto_ranking_coverage_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["before_rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3BCR7ArtifactSet(output_dir, json_path, markdown_path, rows_path)


def build_phase3bc_r7_payload(
    *,
    before_rows: list[dict[str, Any]],
    before_diagnostics: list[dict[str, Any]],
    before_phase3bc_summary: dict[str, Any],
    repair_results: list[dict[str, Any]],
    after_diagnostics: list[dict[str, Any]] | None = None,
    after_phase3bc_summary: dict[str, Any] | None = None,
    freshness_minutes: int = 15,
    repair_rankings: bool = False,
    repair_limit: int = 250,
    limit: int = 2000,
) -> dict[str, Any]:
    generated_at = utc_now()
    after_summary = (
        _coverage_summary(after_diagnostics)
        if after_diagnostics is not None
        else None
    )
    before_summary = _coverage_summary(before_diagnostics)
    repair_counts = Counter(row["status"] for row in repair_results)
    summary = {
        "phase3bc_rows_checked": len(before_rows),
        "active_pure_crypto_rows": before_summary["active_pure_crypto_rows"],
        "current_active_window_rows": before_summary["current_active_window_rows"],
        "expired_crypto_window_rows": before_summary["expired_crypto_window_rows"],
        "fresh_ranking_rows_before": before_summary["fresh_ranking_rows"],
        "missing_or_stale_ranking_rows_before": before_summary[
            "missing_or_stale_ranking_rows"
        ],
        "repairable_ranking_rows_before": before_summary["repairable_ranking_rows"],
        "rankings_inserted": repair_counts.get("RANKING_INSERTED", 0),
        "repair_skipped_rows": sum(
            count for status, count in repair_counts.items() if status != "RANKING_INSERTED"
        ),
        "fresh_ranking_rows_after": after_summary["fresh_ranking_rows"]
        if after_summary
        else None,
        "current_active_window_rows_after": after_summary["current_active_window_rows"]
        if after_summary
        else None,
        "expired_crypto_window_rows_after": after_summary["expired_crypto_window_rows"]
        if after_summary
        else None,
        "missing_or_stale_ranking_rows_after": after_summary[
            "missing_or_stale_ranking_rows"
        ]
        if after_summary
        else None,
        "repair_enabled": repair_rankings,
        "main_gap_before": _main_gap(before_diagnostics),
        "main_gap_after": _main_gap(after_diagnostics or [])
        if after_diagnostics is not None
        else None,
    }
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3BC-R7",
        "phase_version": PHASE3BC_R7_VERSION,
        "mode": "PAPER_ONLY_CRYPTO_RANKING_COVERAGE_REPAIR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "settlement_realization": False,
        "model_name": MODEL_NAME,
        "options": {
            "limit": limit,
            "freshness_minutes": freshness_minutes,
            "repair_rankings": repair_rankings,
            "repair_limit": repair_limit,
        },
        "phase3bc_summary_before": before_phase3bc_summary,
        "phase3bc_summary_after": after_phase3bc_summary or {},
        "summary": summary,
        "issue_counts_before": dict(
            sorted(Counter(row["coverage_issue"] for row in before_diagnostics).items())
        ),
        "issue_counts_after": dict(
            sorted(Counter(row["coverage_issue"] for row in (after_diagnostics or [])).items())
        )
        if after_diagnostics is not None
        else {},
        "repair_result_counts": dict(sorted(repair_counts.items())),
        "repair_results": repair_results,
        "before_rows": before_diagnostics,
        "after_rows": after_diagnostics or [],
        "examples": {
            "repairable": [
                row for row in before_diagnostics if row["repairable"]
            ][:50],
            "blocked": [
                row for row in before_diagnostics if not row["repairable"]
            ][:50],
        },
        "recommended_next_action": _recommended_next_action(summary),
        "next_commands": _next_commands(summary),
    }


def classify_phase3bc_r7_rows(
    rows: list[dict[str, Any]],
    *,
    freshness_minutes: int = 15,
    now: Any | None = None,
) -> list[dict[str, Any]]:
    resolved_now = now or utc_now()
    diagnostics: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("active_market") or row.get("structure_status") != "PURE_CRYPTO":
            continue
        snapshot_at = parse_datetime(row.get("latest_snapshot_at"))
        forecast_at = parse_datetime(row.get("latest_forecast_at"))
        ranking_at = parse_datetime(row.get("latest_ranking_at"))
        ticker_close_time = crypto_ticker_close_time_utc(row.get("ticker"))
        expired_crypto_window = (
            ticker_close_time is not None and ticker_close_time <= resolved_now
        )
        snapshot_age = _age_minutes(snapshot_at, now=resolved_now)
        forecast_age = _age_minutes(forecast_at, now=resolved_now)
        ranking_age = _age_minutes(ranking_at, now=resolved_now)
        issue = (
            ISSUE_EXPIRED_CRYPTO_WINDOW
            if expired_crypto_window
            else _coverage_issue(
                snapshot_at=snapshot_at,
                forecast_at=forecast_at,
                ranking_at=ranking_at,
                snapshot_age_minutes=snapshot_age,
                forecast_age_minutes=forecast_age,
                ranking_age_minutes=ranking_age,
                freshness_minutes=freshness_minutes,
            )
        )
        diagnostics.append(
            {
                "ticker": row.get("ticker"),
                "clean_title": row.get("clean_title") or row.get("title"),
                "event_ticker": row.get("event_ticker"),
                "series_ticker": row.get("series_ticker"),
                "readiness_status": row.get("readiness_status"),
                "final_action": row.get("final_action"),
                "best_side": row.get("best_side"),
                "active_window_status": "EXPIRED"
                if expired_crypto_window
                else "CURRENT_OR_UNKNOWN",
                "ticker_close_time_utc": ticker_close_time.isoformat()
                if ticker_close_time
                else None,
                "expected_value": row.get("expected_value"),
                "opportunity_score": row.get("opportunity_score"),
                "latest_snapshot_at": snapshot_at.isoformat() if snapshot_at else None,
                "latest_forecast_at": forecast_at.isoformat() if forecast_at else None,
                "latest_ranking_at": ranking_at.isoformat() if ranking_at else None,
                "snapshot_age_minutes": snapshot_age,
                "forecast_age_minutes": forecast_age,
                "ranking_age_minutes": ranking_age,
                "ranking_lag_after_forecast_minutes": _lag_minutes(ranking_at, forecast_at),
                "coverage_issue": issue,
                "repairable": issue in REPAIRABLE_ISSUES,
                "repair_block_reason": _repair_block_reason(issue),
            }
        )
    diagnostics.sort(key=_diagnostic_sort_key, reverse=True)
    return diagnostics


def _repair_rankings(
    session: Session,
    diagnostics: list[dict[str, Any]],
    *,
    settings: Settings,
    repair_limit: int,
) -> list[dict[str, Any]]:
    paper_settings = learning_paper_settings(settings)
    ranked_at = utc_now()
    results: list[dict[str, Any]] = []
    candidates = [row for row in diagnostics if row.get("repairable")][: max(0, repair_limit)]
    for row in candidates:
        ticker = str(row["ticker"])
        snapshot = _latest_snapshot(session, ticker)
        forecast = _latest_forecast(session, ticker)
        if snapshot is None:
            results.append(_repair_result(row, status="SKIPPED_MISSING_SNAPSHOT"))
            continue
        if forecast is None:
            results.append(_repair_result(row, status="SKIPPED_MISSING_FORECAST"))
            continue
        ranking = build_market_ranking(
            forecast=forecast,
            snapshot=snapshot,
            settings=paper_settings,
            ranked_at=ranked_at,
        )
        record = insert_market_ranking(session, ranking)
        results.append(
            {
                **_repair_result(row, status="RANKING_INSERTED"),
                "ranking_id": record.id,
                "ranked_at": record.ranked_at.isoformat(),
                "best_side": record.best_side,
                "best_price": record.best_price,
                "estimated_edge": record.estimated_edge,
                "opportunity_score": record.opportunity_score,
            }
        )
    session.flush()
    return results


def _coverage_issue(
    *,
    snapshot_at: Any,
    forecast_at: Any,
    ranking_at: Any,
    snapshot_age_minutes: float | None,
    forecast_age_minutes: float | None,
    ranking_age_minutes: float | None,
    freshness_minutes: int,
) -> str:
    if snapshot_at is None:
        return "SNAPSHOT_MISSING"
    if snapshot_age_minutes is not None and snapshot_age_minutes > freshness_minutes:
        return "SNAPSHOT_STALE"
    if forecast_at is None:
        return "FORECAST_MISSING"
    if forecast_age_minutes is not None and forecast_age_minutes > freshness_minutes:
        return "FORECAST_STALE"
    if ranking_at is None:
        return ISSUE_RANKING_MISSING
    if ranking_at < forecast_at:
        return ISSUE_RANKING_BEFORE_FORECAST
    if ranking_age_minutes is not None and ranking_age_minutes > freshness_minutes:
        return ISSUE_RANKING_STALE
    return ISSUE_RANKING_FRESH


def _coverage_summary(diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    current_rows = [
        row for row in diagnostics if row["coverage_issue"] != ISSUE_EXPIRED_CRYPTO_WINDOW
    ]
    expired_rows = [
        row for row in diagnostics if row["coverage_issue"] == ISSUE_EXPIRED_CRYPTO_WINDOW
    ]
    issue_counts = Counter(row["coverage_issue"] for row in current_rows)
    missing_or_stale = sum(
        issue_counts.get(issue, 0)
        for issue in (ISSUE_RANKING_MISSING, ISSUE_RANKING_STALE, ISSUE_RANKING_BEFORE_FORECAST)
    )
    return {
        "active_pure_crypto_rows": len(diagnostics),
        "current_active_window_rows": len(current_rows),
        "expired_crypto_window_rows": len(expired_rows),
        "fresh_ranking_rows": issue_counts.get(ISSUE_RANKING_FRESH, 0),
        "missing_or_stale_ranking_rows": missing_or_stale,
        "repairable_ranking_rows": sum(1 for row in current_rows if row["repairable"]),
    }


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _latest_forecast(session: Session, ticker: str) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == MODEL_NAME)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _age_minutes(value: Any, *, now: Any) -> float | None:
    if value is None:
        return None
    return (now - value).total_seconds() / 60


def _lag_minutes(ranking_at: Any, forecast_at: Any) -> float | None:
    if ranking_at is None or forecast_at is None:
        return None
    return (ranking_at - forecast_at).total_seconds() / 60


def _repair_result(row: dict[str, Any], *, status: str) -> dict[str, Any]:
    return {
        "ticker": row.get("ticker"),
        "clean_title": row.get("clean_title"),
        "status": status,
        "coverage_issue": row.get("coverage_issue"),
    }


def _repair_block_reason(issue: str) -> str | None:
    reasons = {
        "SNAPSHOT_MISSING": "No exact active snapshot exists for the ticker.",
        "SNAPSHOT_STALE": "Latest snapshot is outside the freshness window.",
        "FORECAST_MISSING": "No exact crypto_v2 forecast exists for the ticker.",
        "FORECAST_STALE": "Latest crypto_v2 forecast is outside the freshness window.",
        ISSUE_EXPIRED_CRYPTO_WINDOW: "Crypto ticker close hour has passed.",
        ISSUE_RANKING_FRESH: "Ranking is already fresh.",
    }
    return reasons.get(issue)


def _diagnostic_sort_key(row: dict[str, Any]) -> tuple[int, float, float]:
    priority = {
        ISSUE_RANKING_BEFORE_FORECAST: 7,
        ISSUE_RANKING_MISSING: 6,
        ISSUE_RANKING_STALE: 4,
        "FORECAST_STALE": 3,
        "FORECAST_MISSING": 2,
        "SNAPSHOT_STALE": 1,
        "SNAPSHOT_MISSING": 1,
        ISSUE_RANKING_FRESH: 0,
        ISSUE_EXPIRED_CRYPTO_WINDOW: -1,
    }.get(str(row.get("coverage_issue")), 0)
    return (
        priority,
        float(row.get("ranking_age_minutes") or 0),
        float(row.get("forecast_age_minutes") or 0),
    )


def _main_gap(diagnostics: list[dict[str, Any]]) -> str | None:
    current_rows = [
        row for row in diagnostics if row["coverage_issue"] != ISSUE_EXPIRED_CRYPTO_WINDOW
    ]
    counts = Counter(row["coverage_issue"] for row in current_rows)
    if not counts:
        if diagnostics and any(
            row["coverage_issue"] == ISSUE_EXPIRED_CRYPTO_WINDOW for row in diagnostics
        ):
            return "EXPIRED_CRYPTO_WINDOWS_ONLY"
        return None
    return counts.most_common(1)[0][0]


def _recommended_next_action(summary: dict[str, Any]) -> str:
    if summary.get("main_gap_after") == R5_PRIMARY_EV_NOT_POSITIVE or summary.get(
        "main_gap_before"
    ) == R5_PRIMARY_EV_NOT_POSITIVE:
        return (
            "R5 post-refresh evidence shows snapshots, forecasts, and ranking coverage "
            "are clear; the current blocker is EV_NOT_POSITIVE. Continue the R5 status "
            "watch without creating paper trades."
        )
    if summary["missing_or_stale_ranking_rows_after"] == 0:
        return (
            "Crypto active pure rows have fresh ranking coverage. Continue the R5 watch "
            "and wait for positive EV plus clean execution gates."
        )
    if summary["repair_enabled"] and summary["rankings_inserted"] > 0:
        return (
            "R7 inserted bounded coverage rankings, but some rows remain blocked by "
            "snapshot or forecast freshness. Refresh crypto snapshots/features/forecasts next."
        )
    if summary["repairable_ranking_rows_before"] > 0 and not summary["repair_enabled"]:
        return (
            "Run this command with --repair-rankings to insert bounded coverage rankings "
            "for active pure crypto rows that already have fresh snapshots and forecasts."
        )
    return (
        "Ranking coverage is blocked upstream by missing or stale snapshots/forecasts; "
        "keep the R5 refresh runner active before repairing rankings."
    )


def _next_commands(summary: dict[str, Any]) -> list[str]:
    if summary.get("main_gap_after") == R5_PRIMARY_EV_NOT_POSITIVE or summary.get(
        "main_gap_before"
    ) == R5_PRIMARY_EV_NOT_POSITIVE:
        return ["kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5"]
    commands = [
        (
            "kalshi-bot phase3bc-r7-crypto-ranking-coverage-repair "
            "--output-dir reports/phase3bc_r7 --repair-rankings"
        ),
        "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5",
    ]
    if summary["missing_or_stale_ranking_rows_after"] == 0:
        commands[0] = (
            "kalshi-bot phase3bc-r4-crypto-ev-risk-diagnostics "
            "--output-dir reports/phase3bc_r4"
        )
    return commands


def _apply_r5_truth_alignment(payload: dict[str, Any], *, reports_dir: Path) -> None:
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return
    alignment = apply_r5_truth_to_blocker_summary(
        summary,
        blocker_key="main_gap_before",
        raw_key="raw_main_gap_before",
        reports_dir=reports_dir,
    )
    if not alignment.get("applies"):
        return
    summary["raw_main_gap_after"] = summary.get("main_gap_after")
    summary["main_gap_after"] = alignment["primary_gap_after_refresh"]
    payload["recommended_next_action"] = _recommended_next_action(summary)
    payload["next_commands"] = _next_commands(summary)


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BC-R7 Crypto Ranking Coverage Repair",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        f"- Safety: `{payload['paper_only_safety']}`",
        "- Live/demo execution: blocked",
        "- Order submission/cancel/replace: blocked",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Issue Counts Before", ""])
    for key, value in payload["issue_counts_before"].items():
        lines.append(f"- {key}: `{value}`")
    if payload["issue_counts_after"]:
        lines.extend(["", "## Issue Counts After", ""])
        for key, value in payload["issue_counts_after"].items():
            lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Repair Results", ""])
    if payload["repair_result_counts"]:
        for key, value in payload["repair_result_counts"].items():
            lines.append(f"- {key}: `{value}`")
    else:
        lines.append("No repair was run.")
    lines.extend(
        [
            "",
            "## Top Repairable Rows",
            "",
            "| Ticker | Issue | Forecast age | Ranking age | Title |",
            "|---|---|---:|---:|---|",
        ]
    )
    repairable = payload["examples"]["repairable"]
    if not repairable:
        lines.append("| none | | | | |")
    else:
        for row in repairable[:20]:
            lines.append(
                "| {ticker} | {issue} | {forecast_age} | {ranking_age} | {title} |".format(
                    ticker=row.get("ticker"),
                    issue=row.get("coverage_issue"),
                    forecast_age=_fmt_age(row.get("forecast_age_minutes")),
                    ranking_age=_fmt_age(row.get("ranking_age_minutes")),
                    title=_md(row.get("clean_title")),
                )
            )
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
        ]
    )
    return "\n".join(lines) + "\n"


def _fmt_age(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1f}m"


def _md(value: Any) -> str:
    text = str(value or "")
    return text.replace("|", "\\|").replace("\n", " ")[:180]
