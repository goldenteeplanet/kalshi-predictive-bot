#!/usr/bin/env python3
"""Diagnose sibling quote coverage by bucket position and time to close."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.research.liquidity_window import WINDOWS, window_for_hours

MIN_WINDOW_OBSERVATIONS = 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    rows = list(
        connection.execute(
            """SELECT c.event_ticker,c.captured_at,c.two_sided_coverage,c.bounds_json,
                      x.close_time
               FROM crypto_event_liquidity_coverage c
               LEFT JOIN (
                 SELECT event_ticker,MAX(close_time) close_time
                 FROM markets WHERE event_ticker IS NOT NULL GROUP BY event_ticker
               ) x ON x.event_ticker=c.event_ticker
               ORDER BY c.captured_at"""
        )
    )
    connection.close()
    payload = diagnose(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "observations"}))
    return 0


def diagnose(rows: list[Any]) -> dict[str, Any]:
    windows: dict[str, list[float]] = defaultdict(list)
    positions: dict[str, list[dict[str, bool]]] = defaultdict(list)
    observations = []
    excluded = 0
    for row in rows:
        captured = _timestamp(row["captured_at"])
        close = _timestamp(row["close_time"])
        if captured is None or close is None:
            excluded += 1
            continue
        hours = (close - captured).total_seconds() / 3600.0
        if hours < 0:
            excluded += 1
            continue
        band = window_for_hours(hours)
        coverage = float(row["two_sided_coverage"])
        windows[band].append(coverage)
        bounds = json.loads(row["bounds_json"] or "{}").get("buckets", [])
        for index, bucket in enumerate(bounds):
            position = bucket.get("kind") or (
                "lower_tail"
                if index == 0
                else "upper_tail"
                if index == len(bounds) - 1
                else "interior"
            )
            has_yes_bid = bool(
                bucket.get("has_bid", float(bucket.get("lower", 0.0)) > 0.0)
            )
            has_no_bid_complement = bool(
                bucket.get("has_ask", float(bucket.get("upper", 1.0)) < 1.0)
            )
            positions[position].append(
                {
                    "two_sided": has_yes_bid and has_no_bid_complement,
                    "yes_bid": has_yes_bid,
                    "no_bid_complement": has_no_bid_complement,
                    "no_side_only": not has_yes_bid and has_no_bid_complement,
                }
            )
        observations.append(
            {
                "event_ticker": row["event_ticker"],
                "hours_to_close": round(hours, 3),
                "window": band,
                "simultaneous_two_sided_coverage": coverage,
            }
        )
    window_rows = []
    window_definitions = {name: (lower, upper) for name, lower, upper in WINDOWS}
    for name, lower, upper in WINDOWS:
        values = windows.get(name, [])
        window_rows.append(
            {
                "window": name,
                "lower_hours": lower,
                "upper_hours": upper,
                "observations": len(values),
                "average_simultaneous_two_sided_coverage": (
                    sum(values) / len(values) if values else None
                ),
                "maximum_simultaneous_two_sided_coverage": max(values) if values else None,
                "gate_clear_rate": (
                    sum(value >= 0.80 for value in values) / len(values) if values else None
                ),
            }
        )
    observed = [row for row in window_rows if row["observations"]]
    best = max(
        observed,
        key=lambda row: (
            row["average_simultaneous_two_sided_coverage"],
            row["observations"],
            -(window_definitions[row["window"]][0]),
        ),
        default=None,
    )
    position_rows = [
        {
            "position": name,
            "bucket_observations": len(values),
            "two_sided_rate": sum(row["two_sided"] for row in values) / len(values),
            "missing_side_rate": 1.0
            - sum(row["two_sided"] for row in values) / len(values),
            "native_yes_bid_rate": sum(row["yes_bid"] for row in values) / len(values),
            "no_bid_complement_yes_ask_rate": sum(
                row["no_bid_complement"] for row in values
            )
            / len(values),
            "no_side_only_upper_bound_rate": sum(
                row["no_side_only"] for row in values
            )
            / len(values),
        }
        for name, values in sorted(positions.items())
    ]
    return {
        "policy": "MAXIMIZE_SIMULTANEOUS_TWO_SIDED_SIBLING_COVERAGE",
        "orderbook_representation": "NATIVE_YES_AND_NO_BIDS_WITH_COMPLEMENT_DERIVED_ASKS",
        "yes_bound_conversion": {
            "lower": "NATIVE_YES_BID_ELSE_ZERO",
            "upper": "ONE_MINUS_NATIVE_NO_BID_ELSE_ONE",
            "no_ask_independence": "NO_ASK_IS_DERIVED_FROM_YES_BID_NOT_A_SECOND_SOURCE",
        },
        "observations_reviewed": len(observations),
        "observations_excluded": excluded,
        "minimum_window_observations": MIN_WINDOW_OBSERVATIONS,
        "scheduling_enabled": bool(
            best and best["observations"] >= MIN_WINDOW_OBSERVATIONS
        ),
        "recommended_window": best,
        "window_diagnostics": window_rows,
        "bucket_position_diagnostics": position_rows,
        "observations": observations,
    }


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
