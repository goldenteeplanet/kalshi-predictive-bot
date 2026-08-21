#!/usr/bin/env python3
"""Research-only chronological range-probability validation."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path


def cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def probability(row: tuple, scale: float) -> float:
    _, _, price, vol, hours, low, high, _, _ = row
    sigma = max(1e-9, price * vol * math.sqrt(max(hours, 1 / 60)) * scale)
    return max(0.001, min(0.999, cdf((high - price) / sigma) - cdf((low - price) / sigma)))


def brier(rows: list[tuple], scale: float) -> float:
    return sum((probability(r, scale) - r[8]) ** 2 for r in rows) / len(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--database", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cost", type=float, default=0.02)
    a = p.parse_args()
    c = sqlite3.connect(f"file:{a.database}?mode=ro", uri=True)
    sql = """
        WITH ranked AS (
          SELECT f.*,
            row_number() OVER (
              PARTITION BY f.ticker ORDER BY f.forecasted_at, f.id
            ) rn
          FROM forecasts f JOIN settlements s ON s.ticker=f.ticker
          WHERE f.model_name='crypto_v2' AND s.result IN ('yes','no')
            AND json_extract(
              f.feature_json, '$.structured_terms.components[0].comparator'
            )='RANGE'
        ), base AS (
          SELECT f.ticker, f.forecasted_at,
            cast(cf.price AS real) price,
            cast(cf.volatility_1h AS real) volatility_1h,
            max(1.0, (julianday(m.close_time)-julianday(f.forecasted_at))*24)
              horizon_hours,
            cast(json_extract(m.raw_json,'$.floor_strike') AS real) floor_strike,
            cast(json_extract(m.raw_json,'$.cap_strike') AS real) cap_strike,
            cast((SELECT ms.best_yes_ask FROM market_snapshots ms
                  WHERE ms.ticker=f.ticker
                    AND ms.captured_at<=f.forecasted_at
                  ORDER BY ms.captured_at DESC, ms.id DESC LIMIT 1) AS real)
              best_yes_ask,
            CASE s.result WHEN 'yes' THEN 1.0 ELSE 0.0 END outcome
          FROM ranked f
          JOIN markets m ON m.ticker=f.ticker
          JOIN settlements s ON s.ticker=f.ticker
          JOIN crypto_features cf ON cf.id=cast(
            json_extract(f.feature_json,'$.crypto_feature_id') AS integer
          )
          WHERE f.rn=1
        )
        SELECT * FROM base
        WHERE price>0 AND volatility_1h>0 AND cap_strike>floor_strike
          AND best_yes_ask>0
        ORDER BY forecasted_at, ticker
    """
    rows = list(c.execute(sql))
    c.close()
    cut = max(1, int(len(rows) * 0.7))
    train, valid = rows[:cut], rows[cut:]
    scales = [0.25, 0.5, 1, 2, 4]
    chosen = min(scales, key=lambda x: brier(train, x))
    vp = [probability(r, chosen) for r in valid]
    edges = [p - r[7] - a.cost for p, r in zip(vp, valid, strict=True)]
    trades = [(e, r) for e, r in zip(edges, valid, strict=True) if e > 0]
    pnl = sum((r[8] - r[7] - a.cost) for _, r in trades)
    out = {
        "status": "EXPERIMENTAL",
        "dedupe": "EARLIEST_FORECAST_PER_TICKER",
        "train_n": len(train),
        "validation_n": len(valid),
        "scale_grid": scales,
        "chosen_scale": chosen,
        "train_brier": brier(train, chosen),
        "validation_brier": sum((p - r[8]) ** 2 for p, r in zip(vp, valid, strict=True))
        / len(valid)
        if valid
        else None,
        "cost_haircut": a.cost,
        "positive_executable_edge_validation": len(trades),
        "validation_simulated_pnl": pnl,
        "promotion_allowed": False,
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
