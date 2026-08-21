#!/usr/bin/env python3
"""Run long, read-only forecast/settlement lineage counts on an isolated DB copy."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def scalar(connection: sqlite3.Connection, sql: str) -> int:
    return int(connection.execute(sql).fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=60)
    payload = {
        "database_mode": "READ_ONLY_ISOLATED_COPY",
        "join_policy": "EXACT_TICKER_ONLY",
        "crypto_forecast_tickers": scalar(
            connection,
            "SELECT COUNT(DISTINCT ticker) FROM forecasts WHERE model_name='crypto_v2'",
        ),
        "crypto_forecast_market_tickers": scalar(
            connection,
            "SELECT COUNT(DISTINCT f.ticker) FROM forecasts f "
            "JOIN markets m ON m.ticker=f.ticker WHERE f.model_name='crypto_v2'",
        ),
        "exact_settled_crypto_forecast_tickers": scalar(
            connection,
            "SELECT COUNT(DISTINCT f.ticker) FROM forecasts f "
            "JOIN settlements s ON s.ticker=f.ticker "
            "WHERE f.model_name='crypto_v2' AND s.result IN ('yes','no')",
        ),
        "guarded_paper_orders": scalar(connection, "SELECT COUNT(*) FROM paper_orders"),
    }
    connection.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
