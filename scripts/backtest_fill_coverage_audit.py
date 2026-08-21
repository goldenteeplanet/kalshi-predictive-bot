#!/usr/bin/env python3
"""Audit no-lookahead executable snapshot coverage for settled forecasts."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--model-name", default="crypto_v2")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=60)
    query = """
      WITH eligible AS (
        SELECT f.id, f.ticker, f.forecasted_at, CAST(f.yes_probability AS REAL) AS p
        FROM forecasts f JOIN settlements s ON s.ticker=f.ticker
        WHERE f.model_name=? AND s.result IN ('yes','no')
          AND f.forecasted_at >= datetime('now', ?)
      ), coverage AS (
        SELECT e.*,
          (SELECT ms.best_yes_ask FROM market_snapshots ms
           WHERE ms.ticker=e.ticker AND ms.captured_at <= e.forecasted_at
           ORDER BY ms.captured_at DESC, ms.id DESC LIMIT 1) AS ask,
          (SELECT ms.captured_at FROM market_snapshots ms
           WHERE ms.ticker=e.ticker AND ms.captured_at <= e.forecasted_at
           ORDER BY ms.captured_at DESC, ms.id DESC LIMIT 1) AS snapshot_at
        FROM eligible e
      )
      SELECT COUNT(*),
        SUM(snapshot_at IS NOT NULL),
        SUM(ask IS NOT NULL AND CAST(ask AS REAL) > 0),
        SUM(ask IS NOT NULL AND p - CAST(ask AS REAL) > 0)
      FROM coverage
    """
    row = connection.execute(
        query, (args.model_name, f"-{args.days} days")
    ).fetchone()
    connection.close()
    payload = {
        "days": args.days,
        "model_name": args.model_name,
        "join_policy": "EXACT_TICKER_ONLY",
        "snapshot_policy": "LATEST_CAPTURED_AT_OR_BEFORE_FORECAST",
        "eligible_forecasts": row[0] or 0,
        "with_pre_forecast_snapshot": row[1] or 0,
        "with_executable_yes_ask": row[2] or 0,
        "positive_gross_edge_before_costs": row[3] or 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
