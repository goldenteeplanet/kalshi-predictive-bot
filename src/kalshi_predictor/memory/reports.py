from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    Forecast,
    ForecastMemory,
    MarketSnapshot,
    MemoryArchiveManifest,
    MemoryEventQuarantine,
    PaperFill,
    PaperOrder,
    Settlement,
)
from kalshi_predictor.memory.repository import memory_status


def memory_health(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    counts = memory_status(session)
    source_counts = {
        "market_snapshots": _count(session, MarketSnapshot),
        "forecasts": _count(session, Forecast),
        "paper_orders": _count(session, PaperOrder),
        "paper_fills": _count(session, PaperFill),
        "settlements": _count(session, Settlement),
    }
    issues: list[str] = []
    if not resolved.phase_3o_market_memory_enabled:
        issues.append("Phase 3O capture is disabled.")
    if resolved.phase_3o_market_memory_mode == "disabled":
        issues.append("PHASE_3O_MARKET_MEMORY_MODE=disabled.")
    if counts["quarantine"]:
        issues.append(f"{counts['quarantine']} memory conflicts are quarantined.")
    if source_counts["forecasts"] and not counts["forecast_memory"]:
        issues.append("Forecast source rows exist but forecast_memory is empty.")
    if source_counts["market_snapshots"] and not counts["market_memory"]:
        issues.append("Market snapshots exist but market_memory is empty.")
    if source_counts["paper_orders"] and not counts["trade_memory"]:
        issues.append("Paper orders exist but trade_memory is empty.")

    status = "READY"
    if not resolved.phase_3o_market_memory_enabled:
        status = "DISABLED"
    elif issues:
        status = "WARNING"

    return {
        "status": status,
        "enabled": resolved.phase_3o_market_memory_enabled,
        "mode": resolved.phase_3o_market_memory_mode,
        "schema_version": resolved.phase_3o_schema_version,
        "data_mode": resolved.phase_3o_default_data_mode,
        "counts": counts,
        "source_counts": source_counts,
        "latest_archive": _latest_archive(session),
        "latest_quarantine": _latest_quarantine(session),
        "pending_outcomes": _pending_outcomes(session),
        "missing_authoritative_inputs": missing_authoritative_inputs(),
        "issues": issues,
    }


def generate_memory_report(
    session: Session,
    *,
    output_path: str | Path = "reports/market_memory_report.md",
    settings: Settings | None = None,
) -> Path:
    payload = memory_health(session, settings=settings)
    text = render_memory_report(payload)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def render_memory_report(payload: dict[str, Any]) -> str:
    counts = payload["counts"]
    source_counts = payload["source_counts"]
    lines = [
        "# Phase 3O Market Memory Report",
        "",
        "## Overall Status",
        "",
        f"- Status: {payload['status']}",
        f"- Capture enabled: {payload['enabled']}",
        f"- Capture mode: {payload['mode']}",
        f"- Schema version: {payload['schema_version']}",
        f"- Default data mode: {payload['data_mode']}",
        "",
        "## Memory Counts",
        "",
        f"- market_memory: {counts['market_memory']}",
        f"- forecast_memory: {counts['forecast_memory']}",
        f"- trade_memory: {counts['trade_memory']}",
        f"- memory_event_quarantine: {counts['quarantine']}",
        f"- latest market event: {counts['latest_market_event']}",
        f"- latest forecast event: {counts['latest_forecast_event']}",
        f"- latest trade event: {counts['latest_trade_event']}",
        "",
        "## Source Reconciliation",
        "",
        f"- market_snapshots: {source_counts['market_snapshots']}",
        f"- forecasts: {source_counts['forecasts']}",
        f"- paper_orders: {source_counts['paper_orders']}",
        f"- paper_fills: {source_counts['paper_fills']}",
        f"- settlements: {source_counts['settlements']}",
        "",
        "## Outcome Status",
        "",
        f"- Pending forecast outcomes: {payload['pending_outcomes']}",
        f"- Latest archive: {payload['latest_archive'] or 'none'}",
        f"- Latest quarantine: {payload['latest_quarantine'] or 'none'}",
        "",
        "## Issues",
        "",
    ]
    if payload["issues"]:
        lines.extend(f"- {item}" for item in payload["issues"])
    else:
        lines.append("- No memory health issues detected.")
    lines.extend(
        [
            "",
            "## Missing Authoritative Inputs",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in payload["missing_authoritative_inputs"])
    lines.extend(
        [
            "",
            "## Recommended Commands",
            "",
            "- kalshi-bot memory-status",
            "- kalshi-bot memory-report --output reports/market_memory_report.md",
            "- kalshi-bot memory-backfill --dry-run",
            "- kalshi-bot memory-archive --output-dir data/memory_archive",
            (
                "- kalshi-bot memory-dataset "
                "--training-as-of 2026-06-18T00:00:00Z "
                "--output reports/memory_dataset.json"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def missing_authoritative_inputs() -> list[str]:
    return [
        (
            "No durable transactional outbox/event bus exists; Phase 3O currently "
            "uses non-fatal synchronous shadow capture hooks."
        ),
        (
            "Forecast model artifact hashes and training cutoffs are not "
            "authoritative in the existing model registry."
        ),
        (
            "Raw provider payloads remain referenced by table/id and hash; "
            "unrestricted raw payload archival is intentionally avoided."
        ),
        (
            "Live execution is not enabled by this system, so trade_memory live "
            "lifecycle fields remain nullable until a guarded live path exists."
        ),
        (
            "Archive restore is represented by verified JSONL manifests; object "
            "storage lifecycle policy is an operator concern."
        ),
    ]


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _pending_outcomes(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ForecastMemory)
            .where(ForecastMemory.forecast_outcome_status == "PENDING")
        )
        or 0
    )


def _latest_quarantine(session: Session) -> str | None:
    row = session.scalar(
        select(MemoryEventQuarantine).order_by(desc(MemoryEventQuarantine.created_at)).limit(1)
    )
    if row is None:
        return None
    return f"{row.store}:{row.idempotency_key} at {row.created_at.isoformat()}"


def _latest_archive(session: Session) -> str | None:
    row = session.scalar(
        select(MemoryArchiveManifest).order_by(desc(MemoryArchiveManifest.created_at)).limit(1)
    )
    if row is None:
        return None
    return f"{row.archive_id} ({row.status}) at {row.created_at.isoformat()}"
