#!/usr/bin/env python3
"""Resumable exact-ticker settlement harvest for crypto walk-forward history."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.phase3aa_r2 import _harvest_one_ticker


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--target", type=int, default=100)
    parser.add_argument("--eligible-before", type=int)
    parser.add_argument("--max-fetches", type=int, default=500)
    parser.add_argument("--symbol")
    parser.add_argument("--min-market-probability", type=float, default=0.0)
    parser.add_argument("--max-market-probability", type=float, default=1.0)
    parser.add_argument("--delay-seconds", type=float, default=0.15)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    state = _load_state(args.checkpoint)
    attempted = set(state.get("attempted", []))
    cached_candidates = state.get("candidates")
    candidates = (
        cached_candidates
        if isinstance(cached_candidates, list)
        else _candidates(
            args.database,
            attempted,
            args.max_fetches,
            symbol=args.symbol,
            min_market_probability=args.min_market_probability,
            max_market_probability=args.max_market_probability,
        )
    )
    candidates = [row for row in candidates if row.get("ticker") not in attempted]
    _write_state(args.checkpoint, attempted, candidates)
    before = (
        args.eligible_before
        if args.eligible_before is not None
        else _eligible_count(args.database)
    )
    estimated_eligible = before
    rows: list[dict[str, Any]] = []
    engine = create_engine(
        f"sqlite:///{args.database}", connect_args={"timeout": 120}
    )
    client = KalshiClient()
    try:
        with Session(engine) as session:
            for candidate in candidates:
                if estimated_eligible >= args.target:
                    break
                ticker = candidate["ticker"]
                row = _harvest_one_ticker(session, client, ticker, [])
                row.update(
                    symbol=candidate["symbol"],
                    crypto_feature_id=candidate["crypto_feature_id"],
                    forecasted_at=candidate["forecasted_at"],
                )
                rows.append(row)
                if row.get("exact_settlement_written") and str(
                    row.get("settlement_result")
                ).lower() in {"yes", "no"}:
                    estimated_eligible += 1
                attempted.add(ticker)
                session.commit()
                _write_state(args.checkpoint, attempted, candidates)
                if args.delay_seconds:
                    time.sleep(args.delay_seconds)
    finally:
        client.close()
        engine.dispose()

    after = estimated_eligible
    payload = {
        "mode": "PAPER_ONLY_EXACT_TICKER_CRYPTO_HISTORY_HARVEST",
        "exact_ticker_only": True,
        "historical_features_synthesized": False,
        "eligible_unique_settled_before": before,
        "eligible_unique_settled_after": after,
        "target": args.target,
        "target_met": after >= args.target,
        "candidate_filter": {
            "symbol": args.symbol,
            "min_market_probability": args.min_market_probability,
            "max_market_probability": args.max_market_probability,
        },
        "fetched_this_run": len(rows),
        "status_counts": dict(Counter(row.get("source_fetch_status") for row in rows)),
        "rows": rows,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps({key: value for key, value in payload.items() if key != "rows"}))
    return 0 if payload["target_met"] else 2


def _connect(database: Path, *, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=120)
    return sqlite3.connect(database, timeout=120)


def _eligible_count(database: Path) -> int:
    connection = _connect(database)
    try:
        return int(connection.execute(_ELIGIBLE_COUNT_SQL).fetchone()[0])
    finally:
        connection.close()


def _candidates(
    database: Path,
    attempted: set[str],
    limit: int,
    *,
    symbol: str | None = None,
    min_market_probability: float = 0.0,
    max_market_probability: float = 1.0,
) -> list[dict[str, Any]]:
    connection = _connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            _CANDIDATE_SQL,
            (
                (symbol or "%").upper(),
                min_market_probability,
                max_market_probability,
                max(limit * 3, limit),
            ),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows if row["ticker"] not in attempted][:limit]


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(
    path: Path, attempted: set[str], candidates: list[dict[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"attempted": sorted(attempted), "candidates": candidates}, indent=2)
        + "\n"
    )
    temporary.replace(path)


_ELIGIBLE_COUNT_SQL = """
SELECT count(DISTINCT f.ticker)
FROM forecasts f
JOIN settlements s ON s.ticker=f.ticker AND s.result IN ('yes','no')
JOIN crypto_features cf
  ON cf.id=CAST(json_extract(f.feature_json,'$.crypto_feature_id') AS INTEGER)
WHERE f.model_name='crypto_v2'
"""

_CANDIDATE_SQL = """
WITH linked AS (
  SELECT DISTINCT ticker FROM crypto_market_links
), ranked AS (
  SELECT f.ticker, f.forecasted_at, CAST(f.market_mid_probability AS REAL) market_implied,
    CAST(json_extract(f.feature_json,'$.crypto_feature_id') AS INTEGER) crypto_feature_id,
    row_number() OVER (PARTITION BY f.ticker ORDER BY f.forecasted_at,f.id) rn
  FROM linked l JOIN forecasts f ON f.ticker=l.ticker
  LEFT JOIN settlements s ON s.ticker=f.ticker
  WHERE f.model_name='crypto_v2' AND s.ticker IS NULL
)
SELECT r.ticker,r.forecasted_at,r.crypto_feature_id,cf.symbol,r.market_implied
FROM ranked r JOIN crypto_features cf ON cf.id=r.crypto_feature_id
JOIN markets m ON m.ticker=r.ticker
WHERE r.rn=1
  AND upper(cf.symbol) LIKE ?
  AND r.market_implied >= ? AND r.market_implied < ?
  AND datetime(COALESCE(m.settlement_ts,m.expiration_time,m.close_time)) <= datetime('now')
ORDER BY datetime(COALESCE(m.settlement_ts,m.expiration_time,m.close_time)),r.ticker
LIMIT ?
"""


if __name__ == "__main__":
    raise SystemExit(main())
