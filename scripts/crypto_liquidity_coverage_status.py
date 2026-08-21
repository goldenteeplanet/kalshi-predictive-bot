#!/usr/bin/env python3
"""Report whether any crypto event family reliably supports point probabilities."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from kalshi_predictor.research.liquidity_priority import family_yield_score
from kalshi_predictor.research.probability_polytope import polytope_information

MIN_FAMILY_EVENTS = 5
MIN_COMPLETE_RATE = 0.80
MAX_COHERENCE_MS = 2500
MIN_SETTLED_INTERVAL_COHORT = 10


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--alignment-manifest", type=Path)
    args = parser.parse_args()
    alignment = (
        json.loads(args.alignment_manifest.read_text(encoding="utf-8"))
        if args.alignment_manifest and args.alignment_manifest.exists()
        else {}
    )
    aligned_events = {
        str(row["event_ticker"]): row
        for row in alignment.get("aligned_events", [])
        if row.get("aligned")
    }
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    rows = [
        SimpleNamespace(**dict(row))
        for row in connection.execute(
            "SELECT * FROM crypto_event_liquidity_coverage ORDER BY captured_at DESC"
        )
    ]
    latest = {}
    for row in rows:
        latest.setdefault(row.event_ticker, row)
    event_information = {
        event: polytope_information(json.loads(row.bounds_json).get("buckets", []))
        for event, row in latest.items()
    }
    eligible_events = [
        event
        for event, row in latest.items()
        if row.coherence_ms <= MAX_COHERENCE_MS
        and event_information[event]["gate_passed"]
        and event in aligned_events
    ]
    settled_events: dict[str, str] = {}
    if eligible_events:
        placeholders = ",".join("?" for _ in eligible_events)
        settled_events = {
            row[0]: row[1]
            for row in connection.execute(
                f"""SELECT m.event_ticker, MAX(s.settled_at)
                FROM markets m LEFT JOIN settlements s ON s.ticker=m.ticker
                WHERE m.event_ticker IN ({placeholders})
                GROUP BY m.event_ticker
                HAVING COUNT(m.ticker)>0
                  AND SUM(CASE WHEN s.result IN ('yes','no') THEN 1 ELSE 0 END)=COUNT(m.ticker)""",
                eligible_events,
            )
        }
    connection.close()
    families = defaultdict(list)
    for row in latest.values():
        families[row.family].append(row)
    family_rows = []
    for family, items in sorted(families.items()):
        information_rows = [event_information[row.event_ticker] for row in items]
        complete = sum(row.complete_executable == "true" for row in items)
        coherent = sum(row.coherence_ms <= MAX_COHERENCE_MS for row in items)
        average = sum(float(row.two_sided_coverage) for row in items) / len(items)
        complete_rate = complete / len(items)
        bounds_failure_rate = sum(row.bounds_feasible != "true" for row in items) / len(items)
        coherence_failure_rate = sum(
            row.coherence_ms > MAX_COHERENCE_MS for row in items
        ) / len(items)
        reliable = len(items) >= MIN_FAMILY_EVENTS and complete_rate >= MIN_COMPLETE_RATE
        rejection_reasons = defaultdict(int)
        for row in items:
            if row.coherence_ms > MAX_COHERENCE_MS:
                rejection_reasons["COHERENCE_TOO_WIDE"] += 1
            if row.bounds_feasible != "true":
                rejection_reasons["BOUNDS_NOT_SIMPLEX_FEASIBLE"] += 1
            information = event_information[row.event_ticker]
            if not information["checks"]["mean_tightened_width"]:
                rejection_reasons["POLYTOPE_MEAN_WIDTH_TOO_WIDE"] += 1
            if not information["checks"]["maximum_tightened_width"]:
                rejection_reasons["POLYTOPE_COORDINATE_WIDTH_TOO_WIDE"] += 1
            if not information["checks"]["simplex_volume_ratio_upper_bound"]:
                rejection_reasons["POLYTOPE_VOLUME_TOO_LARGE"] += 1
        interval_eligible = sum(
            row.coherence_ms <= MAX_COHERENCE_MS
            and event_information[row.event_ticker]["gate_passed"]
            and row.event_ticker in aligned_events
            for row in items
        )
        score = family_yield_score(
            average_coverage=average,
            bounds_failure_rate=bounds_failure_rate,
            coherence_failure_rate=coherence_failure_rate,
            bucket_count=round(sum(row.bucket_count for row in items) / len(items)),
            observed_events=len(items),
        )
        primary_blocker = (
            max(
                rejection_reasons,
                key=lambda reason: (
                    rejection_reasons[reason],
                    reason == "POLYTOPE_VOLUME_TOO_LARGE",
                ),
            )
            if rejection_reasons
            else None
        )
        family_rows.append(
            {
                "family": family,
                "unique_events": len(items),
                "average_two_sided_coverage": average,
                "average_polytope_mean_width": sum(
                    float(row["mean_tightened_width"] or 0.0)
                    for row in information_rows
                )
                / len(information_rows),
                "maximum_polytope_coordinate_width": max(
                    float(row["maximum_tightened_width"] or 0.0)
                    for row in information_rows
                ),
                "maximum_polytope_volume_ratio_upper_bound": max(
                    float(row["simplex_volume_ratio_upper_bound"] or 0.0)
                    for row in information_rows
                ),
                "complete_events": complete,
                "complete_rate": complete_rate,
                "coherent_events": coherent,
                "interval_eligible_events": interval_eligible,
                "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
                "liquidity_diagnostic_counts": {
                    "COMPLETE_EXECUTABLE_VECTOR_MISSING": len(items) - complete,
                    "TWO_SIDED_COVERAGE_BELOW_80_PERCENT": sum(
                        float(row.two_sided_coverage) < 0.80 for row in items
                    ),
                },
                "collection_yield_score": score,
                "primary_blocker": primary_blocker,
                "reliable_complete_distribution": reliable,
            }
        )
    family_rows.sort(key=lambda row: (-row["collection_yield_score"], row["family"]))
    for rank, row in enumerate(family_rows, start=1):
        row["collection_priority_rank"] = rank
    reliable = [row for row in family_rows if row["reliable_complete_distribution"]]
    eta = estimate_time_to_target(
        list(settled_events.values()),
        current=len(settled_events),
        target=MIN_SETTLED_INTERVAL_COHORT,
    )
    payload = {
        "policy": "LIQUIDITY_FIRST_COMPLETE_POINTS_ELSE_EXPLICIT_BOUNDS",
        "minimum_family_events": MIN_FAMILY_EVENTS,
        "minimum_complete_rate": MIN_COMPLETE_RATE,
        "eligibility_policy": "POLYTOPE_INFORMATION_TIGHTENED_WIDTH_AND_VOLUME",
        "polytope_thresholds": next(
            (value["thresholds"] for value in event_information.values()), {}
        ),
        "maximum_coherence_ms": MAX_COHERENCE_MS,
        "unique_events_measured": len(latest),
        "interval_eligible_events": len(eligible_events),
        "forecast_polytope_aligned_events": len(aligned_events),
        "alignment_policy": alignment.get(
            "policy", "MISSING_ALIGNMENT_MANIFEST_FAIL_CLOSED"
        ),
        "alignment_rejection_reason_counts": alignment.get(
            "rejection_reason_counts", {}
        ),
        "settled_interval_eligible_events": len(settled_events),
        "minimum_settled_interval_cohort": MIN_SETTLED_INTERVAL_COHORT,
        "statistically_valid_cohort": len(settled_events)
        >= MIN_SETTLED_INTERVAL_COHORT,
        "persistent_accumulation_active": len(settled_events)
        < MIN_SETTLED_INTERVAL_COHORT,
        "remaining_settled_events": max(
            0, MIN_SETTLED_INTERVAL_COHORT - len(settled_events)
        ),
        "time_to_target_estimate": eta,
        "reliable_complete_families": len(reliable),
        "market_comparison_mode": (
            "POINT_PROBABILITIES" if reliable else "GATED_BID_ASK_PROBABILITY_BOUNDS"
        ),
        "missing_side_policy": "BOUND_TO_ZERO_OR_ONE_NEVER_IMPUTE_A_POINT",
        "families": family_rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "families"}))
    return 0


def estimate_time_to_target(
    settled_at_values: list[str], *, current: int, target: int
) -> dict[str, object]:
    remaining = max(0, target - current)
    if remaining == 0:
        return {
            "status": "COMPLETE",
            "estimated_days": 0.0,
            "estimated_completion_at": datetime.now(UTC).isoformat(),
            "confidence": "COMPLETE",
            "method": "TARGET_REACHED",
        }
    timestamps = sorted(
        parsed
        for value in settled_at_values
        if value and (parsed := _timestamp(value)) is not None
    )
    if len(timestamps) < 2:
        return {
            "status": "INSUFFICIENT_SETTLED_HISTORY",
            "estimated_days": None,
            "estimated_completion_at": None,
            "confidence": "UNAVAILABLE",
            "method": "NEED_AT_LEAST_TWO_ELIGIBLE_SETTLEMENTS",
        }
    span_seconds = (timestamps[-1] - timestamps[0]).total_seconds()
    interval_seconds = max(1.0, span_seconds / (len(timestamps) - 1))
    remaining_seconds = interval_seconds * remaining
    return {
        "status": "ESTIMATED",
        "estimated_days": round(remaining_seconds / 86400.0, 2),
        "estimated_completion_at": (
            datetime.now(UTC) + timedelta(seconds=remaining_seconds)
        ).isoformat(),
        "confidence": "PRELIMINARY" if len(timestamps) < 5 else "OBSERVED",
        "method": "MEAN_INTERVAL_BETWEEN_ELIGIBLE_SETTLEMENTS",
        "observed_settlements": len(timestamps),
    }


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
