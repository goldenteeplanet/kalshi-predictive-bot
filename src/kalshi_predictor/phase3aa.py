from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.confidence.engine import run_model_confidence_engine
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.ingest.markets import sync_settlements
from kalshi_predictor.lanes.metrics import refresh_learning_metrics
from kalshi_predictor.paper.models import PnlSummary
from kalshi_predictor.paper.pnl import calculate_and_store_pnl
from kalshi_predictor.paper.settlement_reconciliation import (
    PAPER_ONLY_SAFETY,
    build_paper_settlement_reconciliation,
)
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class Phase3AAArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


SyncJob = Callable[[Session, Settings], int]
PnlJob = Callable[[Session, Settings], PnlSummary]
ConfidenceJob = Callable[[Session, Settings], Any]
LearningMetricsJob = Callable[[Session, Settings], Any]


def build_settlement_eta_schedule(
    session: Session,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    session.flush()
    reconciliation = build_paper_settlement_reconciliation(session, limit=limit)
    rows = reconciliation["rows"]
    active_unsettled = [
        row for row in rows if row["status"] == "FILLED" and not row["settlement_found"]
    ]
    due_rows = [
        row
        for row in active_unsettled
        if row.get("close_time_bucket") in {"overdue", "0-6h"}
    ]
    eta_buckets = reconciliation["close_time_buckets"]
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AA",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "summary": {
            "orders_reviewed": reconciliation["summary"]["orders_reviewed"],
            "active_unsettled": len(active_unsettled),
            "due_or_overdue": len(due_rows),
            "eligible_exact_settlements": reconciliation["summary"]["eligible_to_settle_now"],
            "eta_buckets": eta_buckets,
        },
        "recommended_watch_intervals": _watch_intervals(eta_buckets),
        "due_or_overdue_tickers": [_watch_row(row) for row in due_rows[:100]],
        "next_to_settle": [_watch_row(row) for row in active_unsettled[:100]],
        "eligible_trades": reconciliation["eligible_trades"],
        "reconciliation_top_reason": reconciliation["top_reason"],
        "recommended_next_action": _eta_next_action(reconciliation, due_rows),
    }


def run_paper_outcome_realizer(
    session: Session,
    *,
    settings: Settings | None = None,
    sync: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    sync_job: SyncJob | None = None,
    pnl_job: PnlJob | None = None,
    confidence_job: ConfidenceJob | None = None,
    learning_metrics_job: LearningMetricsJob | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    before = build_paper_settlement_reconciliation(session, limit=limit)
    synced = 0
    if sync and not dry_run:
        synced = (sync_job or _sync_settlements_job)(session, resolved)
    after_sync = build_paper_settlement_reconciliation(session, limit=limit)
    eligible = after_sync["summary"]["eligible_to_settle_now"]
    should_realize = eligible > 0 and not dry_run

    pnl_summary: PnlSummary | None = None
    confidence_summary: dict[str, Any] | None = None
    learning_metric_summary: dict[str, Any] | None = None
    if should_realize:
        pnl_summary = (pnl_job or _paper_pnl_job)(session, resolved)
        confidence_result = (confidence_job or _confidence_job)(session, resolved)
        confidence_summary = _confidence_summary(confidence_result)
        learning_metric = (learning_metrics_job or _learning_metrics_job)(session, resolved)
        learning_metric_summary = _object_summary(learning_metric)

    after_realize = build_paper_settlement_reconciliation(session, limit=limit)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AA",
        "mode": "PAPER_ONLY_OUTCOME_REALIZER",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "dry_run": dry_run,
        "sync_requested": sync,
        "settlements_synced": synced,
        "eligible_before": before["summary"]["eligible_to_settle_now"],
        "eligible_after_sync": eligible,
        "eligible_after_realize": after_realize["summary"]["eligible_to_settle_now"],
        "pnl_realized": should_realize,
        "pnl_summary": _pnl_summary(pnl_summary),
        "confidence_summary": confidence_summary,
        "learning_metric_summary": learning_metric_summary,
        "eta_schedule": build_settlement_eta_schedule(session, limit=limit),
        "recommended_next_action": _realizer_next_action(eligible, dry_run, sync),
    }


def write_phase3aa_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3aa"),
    settings: Settings | None = None,
    sync: bool = False,
    dry_run: bool = True,
    limit: int | None = None,
) -> Phase3AAArtifactSet:
    payload = run_paper_outcome_realizer(
        session,
        settings=settings,
        sync=sync,
        dry_run=dry_run,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3aa_outcome_realizer.json"
    markdown_path = output_dir / "phase3aa_outcome_realizer.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AAArtifactSet(output_dir, json_path, markdown_path)


def _sync_settlements_job(session: Session, settings: Settings) -> int:
    del settings
    return sync_settlements(lookback_days=30, limit=100, max_pages=5, session=session)


def _paper_pnl_job(session: Session, settings: Settings) -> PnlSummary:
    del settings
    return calculate_and_store_pnl(session)


def _confidence_job(session: Session, settings: Settings) -> Any:
    return run_model_confidence_engine(session, settings=settings)


def _learning_metrics_job(session: Session, settings: Settings) -> Any:
    return refresh_learning_metrics(session, settings=settings)


def _watch_intervals(eta_buckets: dict[str, int]) -> list[dict[str, Any]]:
    intervals = [
        ("overdue", 5, "Sync settlements frequently; markets may already be settled."),
        ("0-6h", 5, "Near close; check settlements and P&L often."),
        ("6-24h", 15, "Good overnight learning bucket."),
        ("1-2d", 60, "Useful, but slower than tonight data capture."),
        ("2-3d", 120, "Keep watching; do not overfill this bucket."),
        ("3-7d", 360, "Deprioritize for Learning Mode."),
        ("7d+", 720, "Avoid for fast learning unless opportunity quality is exceptional."),
        ("unknown", 720, "Avoid until close-time metadata is available."),
    ]
    return [
        {
            "bucket": bucket,
            "trades": eta_buckets.get(bucket, 0),
            "interval_minutes": minutes,
            "policy": policy,
        }
        for bucket, minutes, policy in intervals
        if eta_buckets.get(bucket, 0)
    ]


def _watch_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper_order_id": row["paper_order_id"],
        "ticker": row["ticker"],
        "model_name": row["model_name"],
        "close_time_bucket": row.get("close_time_bucket"),
        "hours_to_close": row.get("hours_to_close"),
        "market_status": row.get("market_status"),
        "reason": row.get("reason"),
    }


def _eta_next_action(reconciliation: dict[str, Any], due_rows: list[dict[str, Any]]) -> str:
    if reconciliation["summary"]["eligible_to_settle_now"]:
        return "Run phase3aa-realize without --dry-run to refresh paper P&L."
    if due_rows:
        return "Run sync-settlements and phase3aa-realize around the due/overdue close buckets."
    return "Keep Learning Mode focused on 0-24h markets until exact ticker settlements arrive."


def _realizer_next_action(eligible: int, dry_run: bool, sync: bool) -> str:
    if dry_run:
        return "Dry run complete. Rerun without --dry-run when exact settlements are eligible."
    if eligible > 0:
        return "Paper P&L and learning metrics refreshed from exact ticker settlements."
    if not sync:
        return "No exact settlements found locally. Rerun with --sync-settlements."
    return "No exact settlements found after sync; keep settlement watcher running."


def _pnl_summary(summary: PnlSummary | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "positions_evaluated": summary.positions_evaluated,
        "pnl_rows_inserted": summary.pnl_rows_inserted,
        "realized_pnl": str(summary.realized_pnl),
        "unrealized_pnl": str(summary.unrealized_pnl),
        "total_pnl": str(summary.total_pnl),
    }


def _confidence_summary(value: Any) -> dict[str, Any]:
    return {
        "scores_inserted": getattr(value, "scores_inserted", None),
        "weights_inserted": getattr(value, "weights_inserted", None),
        "rows": len(getattr(value, "rows", []) or []),
    }


def _object_summary(value: Any) -> dict[str, Any]:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return {"value": str(value)}


def _render_markdown(payload: dict[str, Any]) -> str:
    eta = payload["eta_schedule"]
    lines = [
        "# Phase 3AA Settlement ETA + Paper Outcome Realizer",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Dry run: {payload['dry_run']}",
        f"- Settlements synced: {payload['settlements_synced']}",
        f"- Eligible after sync: {payload['eligible_after_sync']}",
        f"- Paper P&L realized: {payload['pnl_realized']}",
        "",
        "## ETA Summary",
        "",
    ]
    for key, value in eta["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Watch Intervals",
            "",
            "| Bucket | Trades | Interval minutes | Policy |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for row in eta["recommended_watch_intervals"]:
        lines.append(
            f"| {row['bucket']} | {row['trades']} | {row['interval_minutes']} | "
            f"{row['policy']} |"
        )
    if not eta["recommended_watch_intervals"]:
        lines.append("| None | 0 | 0 | No active unsettled paper trades. |")
    lines.extend(
        [
            "",
            "## Due Or Overdue",
            "",
            "| Order | Ticker | Bucket | Hours | Status |",
            "| ---: | --- | --- | ---: | --- |",
        ]
    )
    if eta["due_or_overdue_tickers"]:
        for row in eta["due_or_overdue_tickers"][:30]:
            lines.append(
                f"| {row['paper_order_id']} | {row['ticker']} | "
                f"{row['close_time_bucket']} | {row['hours_to_close']} | "
                f"{row['market_status']} |"
            )
    else:
        lines.append("|  | None |  |  | No due or overdue exact-ticker candidates. |")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)
