from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    ForecastMemory,
    MarketMemory,
    MemoryArchiveManifest,
    TradeMemory,
)
from kalshi_predictor.memory.contracts import stable_id
from kalshi_predictor.utils.time import utc_now


def archive_memory_to_jsonl(
    session: Session,
    *,
    output_dir: str | Path | None = None,
    settings: Settings | None = None,
) -> MemoryArchiveManifest:
    resolved = settings or get_settings()
    root = Path(output_dir or resolved.phase_3o_archive_dir)
    root.mkdir(parents=True, exist_ok=True)
    archive_id = stable_id("memory_archive", utc_now().isoformat())
    stores = {
        "market_memory": (MarketMemory, root / "market_memory.jsonl"),
        "forecast_memory": (ForecastMemory, root / "forecast_memory.jsonl"),
        "trade_memory": (TradeMemory, root / "trade_memory.jsonl"),
    }
    counts: dict[str, int] = {}
    checksums: dict[str, str] = {}
    for store, (model, path) in stores.items():
        rows = list(session.scalars(select(model).order_by(model.recorded_at)))
        hasher = hashlib.sha256()
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                payload = _row_payload(row)
                encoded = encode_json(payload)
                handle.write(encoded + "\n")
                hasher.update(encoded.encode("utf-8"))
        counts[store] = len(rows)
        checksums[store] = f"sha256:{hasher.hexdigest()}"
    source_ranges = _source_ranges(session)
    manifest_path = root / "manifest.json"
    manifest_payload = {
        "archive_id": archive_id,
        "format": "jsonl",
        "compression": "none",
        "verified": True,
        "purge_permitted": False,
        "row_counts": counts,
        "checksums": checksums,
        "source_range": source_ranges,
    }
    manifest_path.write_text(encode_json(manifest_payload), encoding="utf-8")
    manifest = MemoryArchiveManifest(
        archive_id=archive_id,
        created_at=utc_now(),
        output_uri=str(root),
        status="VERIFIED",
        row_counts_json=encode_json(counts),
        checksums_json=encode_json(checksums),
        source_range_json=encode_json(source_ranges),
        raw_json=encode_json({**manifest_payload, "manifest_path": str(manifest_path)}),
    )
    session.add(manifest)
    session.flush()
    return manifest


def _row_payload(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _source_ranges(session: Session) -> dict[str, Any]:
    ranges: dict[str, Any] = {}
    for name, model in (
        ("market_memory", MarketMemory),
        ("forecast_memory", ForecastMemory),
        ("trade_memory", TradeMemory),
    ):
        rows = list(session.scalars(select(model.recorded_at).order_by(model.recorded_at)))
        ranges[name] = {
            "start": rows[0].isoformat() if rows else None,
            "end": rows[-1].isoformat() if rows else None,
        }
    return ranges
