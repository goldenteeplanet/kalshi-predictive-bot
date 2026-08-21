#!/usr/bin/env python3
"""Research-only chronological validation for stored forecast probabilities."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def calibrated(probability: float, scale: float) -> float:
    return max(0.001, min(0.999, 0.5 + (probability - 0.5) * scale))


def brier(rows: list[tuple], scale: float) -> float:
    return sum((calibrated(row[2], scale) - row[4]) ** 2 for row in rows) / len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--cost", type=float, default=0.02)
    args = parser.parse_args()

    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=60)
    rows = list(
        connection.execute(
            """
            WITH ranked AS (
              SELECT f.id, f.ticker, f.forecasted_at,
                     CAST(f.yes_probability AS REAL) probability,
                     ROW_NUMBER() OVER (
                       PARTITION BY f.ticker ORDER BY f.forecasted_at, f.id
                     ) rn
              FROM forecasts f
              JOIN settlements s ON s.ticker=f.ticker
              WHERE f.model_name=? AND s.result IN ('yes','no')
            )
            SELECT f.ticker, f.forecasted_at, f.probability,
              CAST((SELECT ms.best_yes_ask FROM market_snapshots ms
                    WHERE ms.ticker=f.ticker
                      AND ms.captured_at<=f.forecasted_at
                    ORDER BY ms.captured_at DESC, ms.id DESC LIMIT 1) AS REAL) ask,
              CASE s.result WHEN 'yes' THEN 1.0 ELSE 0.0 END outcome
            FROM ranked f JOIN settlements s ON s.ticker=f.ticker
            WHERE f.rn=1 AND f.probability BETWEEN 0 AND 1
            ORDER BY f.forecasted_at, f.ticker
            """,
            (args.model_name,),
        )
    )
    connection.close()
    if len(rows) < 4:
        payload = {
            "status": "INSUFFICIENT_COVERAGE",
            "model_name": args.model_name,
            "observations": len(rows),
            "promotion_allowed": False,
        }
    else:
        cut = max(1, int(len(rows) * 0.7))
        train, validation = rows[:cut], rows[cut:]
        scales = [0.25, 0.5, 0.75, 1.0]
        chosen = min(scales, key=lambda scale: brier(train, scale))
        validation_probabilities = [calibrated(row[2], chosen) for row in validation]
        trades = [
            (probability - row[3] - args.cost, row)
            for probability, row in zip(validation_probabilities, validation, strict=True)
            if row[3] is not None and row[3] > 0 and probability - row[3] - args.cost > 0
        ]
        payload = {
            "status": "EXPERIMENTAL",
            "model_name": args.model_name,
            "dedupe": "EARLIEST_FORECAST_PER_TICKER",
            "snapshot_policy": "LATEST_CAPTURED_AT_OR_BEFORE_FORECAST",
            "train_n": len(train),
            "validation_n": len(validation),
            "validation_executable_n": sum(row[3] is not None and row[3] > 0 for row in validation),
            "scale_grid": scales,
            "chosen_scale": chosen,
            "train_brier": brier(train, chosen),
            "validation_brier": sum(
                (probability - row[4]) ** 2
                for probability, row in zip(validation_probabilities, validation, strict=True)
            )
            / len(validation),
            "cost_haircut": args.cost,
            "positive_executable_edge_validation": len(trades),
            "validation_simulated_pnl": sum(row[4] - row[3] - args.cost for _, row in trades),
            "promotion_allowed": False,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
