#!/usr/bin/env python3
"""Fetch exact expired forecast markets into isolated research JSONL storage."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.kalshi.client import KalshiClient, KalshiClientError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    parser.add_argument(
        "--model-name",
        action="append",
        dest="model_names",
        help="Forecast model to harvest; repeat for multiple models (default: crypto_v2).",
    )
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 250:
        raise ValueError("limit must be between 1 and 250")

    completed: set[str] = set()
    if args.output.exists():
        for line in args.output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                completed.add(str(json.loads(line)["requested_ticker"]))

    model_names = tuple(args.model_names or ["crypto_v2"])
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=60)
    placeholders = ",".join("?" for _ in completed) or "''"
    model_placeholders = ",".join("?" for _ in model_names)
    params: tuple[str, ...] = (*model_names, *tuple(sorted(completed)))
    query = f"""
        SELECT DISTINCT f.ticker
        FROM forecasts f
        JOIN markets m ON m.ticker = f.ticker
        LEFT JOIN settlements s ON s.ticker = f.ticker
        WHERE f.model_name IN ({model_placeholders})
          AND m.close_time IS NOT NULL
          AND m.close_time < CURRENT_TIMESTAMP
          AND (s.ticker IS NULL OR s.result NOT IN ('yes', 'no'))
          AND f.ticker NOT IN ({placeholders})
        ORDER BY m.close_time DESC, f.ticker
        LIMIT ?
    """
    tickers = [row[0] for row in connection.execute(query, (*params, args.limit))]
    connection.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fetched = settled = errors = 0
    client = KalshiClient()
    try:
        with args.output.open("a", encoding="utf-8") as target:
            for ticker in tickers:
                record = {
                    "requested_ticker": ticker,
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "identity_policy": "EXACT_TICKER_ONLY",
                    "database_written": False,
                }
                try:
                    market = client.get_market(ticker)
                    returned = str(market.get("ticker") or "")
                    if returned != ticker:
                        raise ValueError(f"exact ticker mismatch: {returned!r}")
                    record.update(
                        {
                            "fetch_status": "OK",
                            "returned_ticker": returned,
                            "status": market.get("status"),
                            "result": market.get("result"),
                            "settlement_value": market.get("settlement_value"),
                            "settlement_ts": market.get("settlement_ts"),
                            "event_ticker": market.get("event_ticker"),
                        }
                    )
                    fetched += 1
                    if market.get("result") in {"yes", "no"}:
                        settled += 1
                except (KalshiClientError, ValueError) as exc:
                    record.update({"fetch_status": "ERROR", "error": str(exc)})
                    errors += 1
                target.write(json.dumps(record, sort_keys=True) + "\n")
                target.flush()
                time.sleep(max(0.0, args.delay_seconds))
    finally:
        client.close()
    print(
        json.dumps(
            {
                "model_names": model_names,
                "selected": len(tickers),
                "fetched": fetched,
                "settled": settled,
                "errors": errors,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
