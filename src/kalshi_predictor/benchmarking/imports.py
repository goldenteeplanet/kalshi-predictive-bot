from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.replay import SyntheticEpisode, load_synthetic_episode
from kalshi_predictor.utils.time import parse_datetime


@dataclass(frozen=True)
class ReplayImportResult:
    episode: SyntheticEpisode | None
    diagnostics: tuple[str, ...]
    source_format: str
    user_owned_data_only: bool = True


def import_user_replay(path: Path) -> ReplayImportResult:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        source_format = "json"
    elif suffix == ".csv":
        payload = _tabular_payload(list(csv.DictReader(path.open(encoding="utf-8"))))
        source_format = "csv"
    elif suffix in {".parquet", ".pq"}:
        try:
            import pandas as pd
            import pyarrow  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Parquet replay import requires the benchmark optional dependency: "
                "pip install -e '.[benchmark]'"
            ) from exc

        payload = _tabular_payload(pd.read_parquet(path).to_dict(orient="records"))
        source_format = "parquet"
    else:
        raise ValueError("Replay import supports JSON, CSV, and Parquet only")
    diagnostics = _diagnostics(payload)
    fatal = any(item.startswith("ERROR:") for item in diagnostics)
    return ReplayImportResult(
        None if fatal else load_synthetic_episode(payload), tuple(diagnostics), source_format
    )


def _tabular_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    events = []
    for row in rows:
        message = row.get("message") or row.get("message_json")
        if isinstance(message, str):
            message = json.loads(message)
        events.append({"timestamp": row.get("timestamp"), "ticker": row.get("ticker"),
                       "kind": row.get("kind"), "message": message})
    return {"episode_id": "user-import", "category": "user", "events": events,
            "settlements": {}}


def _diagnostics(payload: dict[str, Any]) -> list[str]:
    diagnostics = []
    last_timestamp = None
    sequences: dict[str, int] = {}
    seen_snapshot: set[str] = set()
    for index, row in enumerate(payload.get("events", [])):
        timestamp = parse_datetime(row.get("timestamp"))
        ticker = str(row.get("ticker") or "")
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        sequence = message.get("seq")
        if timestamp is None or not ticker or row.get("kind") not in {"snapshot", "delta"}:
            diagnostics.append(f"ERROR:INVALID_SCHEMA_AT_ROW:{index}")
            continue
        if last_timestamp is not None and timestamp < last_timestamp:
            diagnostics.append(f"WARN:OUT_OF_ORDER_TIMESTAMP_AT_ROW:{index}")
        last_timestamp = timestamp
        if row.get("kind") == "snapshot":
            seen_snapshot.add(ticker)
        elif ticker not in seen_snapshot:
            diagnostics.append(f"ERROR:DELTA_BEFORE_SNAPSHOT:{ticker}")
        if sequence is not None:
            sequence = int(sequence)
            previous = sequences.get(ticker)
            if previous is not None and sequence == previous:
                diagnostics.append(f"ERROR:DUPLICATE_SEQUENCE:{ticker}:{sequence}")
            elif previous is not None and row.get("kind") == "delta" and sequence != previous + 1:
                diagnostics.append(f"ERROR:SEQUENCE_GAP:{ticker}:{previous + 1}:{sequence}")
            sequences[ticker] = sequence
    return diagnostics
