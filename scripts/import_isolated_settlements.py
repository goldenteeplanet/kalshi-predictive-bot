#!/usr/bin/env python3
"""Import verified exact settlements into a disposable research database only."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--harvest", type=Path, required=True)
    parser.add_argument("--allow-disposable-write", action="store_true")
    args = parser.parse_args()
    if not args.allow_disposable_write:
        raise ValueError("explicit --allow-disposable-write is required")
    if "research-snapshots" not in str(args.database):
        raise ValueError("refusing to write outside the isolated research-snapshots path")

    rows = [
        json.loads(line)
        for line in args.harvest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    verified = [
        row
        for row in rows
        if row.get("identity_policy") == "EXACT_TICKER_ONLY"
        and row.get("requested_ticker") == row.get("returned_ticker")
        and row.get("result") in {"yes", "no"}
        and row.get("database_written") is False
    ]
    connection = sqlite3.connect(args.database)
    before = connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    inserted = 0
    for row in verified:
        cursor = connection.execute(
            "INSERT INTO settlements "
            "(ticker, result, yes_settlement_value, settled_at, raw_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(ticker) DO UPDATE SET "
            "result=excluded.result, "
            "yes_settlement_value=excluded.yes_settlement_value, "
            "settled_at=excluded.settled_at, raw_json=excluded.raw_json, "
            "updated_at=CURRENT_TIMESTAMP "
            "WHERE settlements.result NOT IN ('yes', 'no')",
            (
                row["requested_ticker"],
                row["result"],
                "1" if row["result"] == "yes" else "0",
                row.get("settlement_ts") or row.get("fetched_at"),
                json.dumps(row, sort_keys=True),
            ),
        )
        inserted += cursor.rowcount
    connection.commit()
    after = connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    connection.close()
    print(
        json.dumps(
            {
                "verified": len(verified),
                "inserted": inserted,
                "before": before,
                "after": after,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
