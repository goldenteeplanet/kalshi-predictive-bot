"""Build a generic outcome-blind settlement hint artifact from research captures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.ingest.settlement_hints import SCHEMA, artifact_hash


def _digest(values: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(values)).encode()).hexdigest()


def build(research_db: Path, *, now: datetime) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{research_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=1")
    evaluated = {
        row[0]
        for row in connection.execute("SELECT capture_id FROM prospective_pair_evaluations")
    }
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT capture_id,ticker,settlement_target,bundle_hash
        FROM prospective_paired_captures
        WHERE settlement_target IS NOT NULL
        ORDER BY settlement_target,ticker,capture_id
        """
    ):
        due_at = datetime.fromisoformat(row["settlement_target"].replace("Z", "+00:00"))
        due_at = due_at.replace(tzinfo=due_at.tzinfo or UTC).astimezone(UTC)
        if row["capture_id"] not in evaluated and due_at <= now.astimezone(UTC):
            grouped[row["ticker"]].append(row)
    connection.close()
    hints = []
    for ticker, rows in grouped.items():
        hints.append(
            {
                "ticker": ticker,
                "due_at": min(row["settlement_target"] for row in rows),
                "source_capture_id_hash": _digest([row["capture_id"] for row in rows]),
                "bundle_set_hash": _digest([row["bundle_hash"] for row in rows]),
            }
        )
    hints.sort(key=lambda row: (str(row["due_at"]), str(row["ticker"])))
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "generated_at": now.astimezone(UTC).isoformat(),
        "hints": hints,
    }
    payload["artifact_hash"] = artifact_hash(payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build(args.research_db, now=datetime.now(UTC))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    hint_rows = payload["hints"]
    hint_count = len(hint_rows) if isinstance(hint_rows, list) else 0
    print(json.dumps({"artifact_hash": payload["artifact_hash"], "hints": hint_count}))


if __name__ == "__main__":
    main()
