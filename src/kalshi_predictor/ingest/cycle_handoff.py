"""Generic committed snapshot-cycle metadata artifacts with no research dependency."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import MarketSnapshot


def build_committed_cycle_metadata(
    source: Session,
    *,
    cycle_window_seconds: int = 5,
    cycle_watermark: datetime | None = None,
) -> dict[str, Any] | None:
    watermark = cycle_watermark or source.scalar(select(func.max(MarketSnapshot.captured_at)))
    if watermark is None:
        return None
    watermark = watermark.replace(tzinfo=UTC) if watermark.tzinfo is None else watermark
    rows = list(
        source.scalars(
            select(MarketSnapshot)
            .where(
                MarketSnapshot.captured_at >= watermark - timedelta(seconds=cycle_window_seconds),
                MarketSnapshot.captured_at <= watermark,
            )
            .order_by(MarketSnapshot.captured_at, MarketSnapshot.ticker, MarketSnapshot.id)
        )
    )
    manifest = [
        {
            "id": row.id,
            "ticker": row.ticker,
            "captured_at": _iso(row.captured_at),
            "status": str(row.status or "").lower(),
            "market_hash": _hash(row.raw_market_json),
            "book_hash": _hash(row.raw_orderbook_json),
        }
        for row in rows
    ]
    return {
        "schema": "kalshi.snapshot-cycle-handoff.v1",
        "cycle_watermark": _iso(watermark),
        "cycle_window_seconds": cycle_window_seconds,
        "snapshot_count": len(rows),
        "cycle_hash": _hash(manifest),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def write_committed_cycle_artifact(
    source: Session, *, output_path: Path, cycle_window_seconds: int = 5
) -> dict[str, Any] | None:
    payload = build_committed_cycle_metadata(source, cycle_window_seconds=cycle_window_seconds)
    if payload is None:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, output_path)
    return payload


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _iso(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()
