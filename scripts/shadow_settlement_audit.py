#!/usr/bin/env python3
"""Audit exact-ticker settlement overlap for isolated shadow observations."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in args.signals.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tickers = sorted({str(row["market_ticker"]) for row in rows})
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    query = "SELECT result FROM settlements WHERE ticker = ? AND result IN ('yes', 'no')"
    settlements: dict[str, str] = {}
    for ticker in tickers:
        result = connection.execute(query, (ticker,)).fetchone()
        if result is not None:
            settlements[ticker] = str(result[0])
    connection.close()

    payload = {
        "join_policy": "EXACT_TICKER_ONLY",
        "database_mode": "READ_ONLY",
        "observations": len(rows),
        "unique_tickers": len(tickers),
        "exact_settled_tickers": len(settlements),
        "settleable_observations": sum(
            str(row["market_ticker"]) in settlements for row in rows
        ),
        "unsettled_unique_tickers": len(tickers) - len(settlements),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
