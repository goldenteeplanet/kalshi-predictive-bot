#!/usr/bin/env python3
"""Install idempotent indexes for event-weighted crypto research queries."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

INDEXES = {
    "ix_markets_event_ticker": (
        "CREATE INDEX IF NOT EXISTS ix_markets_event_ticker ON markets(event_ticker)"
    ),
    "ix_markets_event_status_close": (
        "CREATE INDEX IF NOT EXISTS ix_markets_event_status_close "
        "ON markets(event_ticker,status,close_time)"
    ),
    "ix_crypto_capture_event_completed": (
        "CREATE INDEX IF NOT EXISTS ix_crypto_capture_event_completed "
        "ON crypto_event_quote_captures(event_ticker,completed_at)"
    ),
    "ix_crypto_liquidity_event_coherent": (
        "CREATE INDEX IF NOT EXISTS ix_crypto_liquidity_event_coherent "
        "ON crypto_event_liquidity_coverage(event_ticker,captured_at,coherence_ms,"
        "two_sided_coverage,bounds_feasible)"
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    connection = sqlite3.connect(args.database, timeout=120)
    try:
        connection.execute("PRAGMA busy_timeout=120000")
        existing_tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        installed = []
        skipped = []
        for name, statement in INDEXES.items():
            table = _table_name(statement)
            if table not in existing_tables:
                skipped.append({"index": name, "reason": f"MISSING_TABLE:{table}"})
                continue
            connection.execute(statement)
            installed.append(name)
        connection.commit()
        connection.commit()
    finally:
        connection.close()
    payload = {
        "policy": "IDEMPOTENT_INDEXED_EVENT_CAPTURE_MANIFEST_LOOKUPS",
        "installed_or_verified": installed,
        "skipped": skipped,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload))
    return 0


def _table_name(statement: str) -> str:
    return statement.split(" ON ", 1)[1].split("(", 1)[0].strip()


if __name__ == "__main__":
    raise SystemExit(main())
