#!/usr/bin/env python3
"""Capture complete crypto sibling quote vectors and report cohort progress."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.data.db import session_scope
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.research.event_quote_collector import (
    SERIES_SYMBOLS,
    backfill_registry,
    capture_candidate,
    cohort_status,
    discover_candidates_from_registry,
    refresh_targeted_event_forecasts,
    select_candidates_for_liquidity_window,
    select_candidates_with_fresh_forecasts,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--series", default=",".join(SERIES_SYMBOLS))
    parser.add_argument("--coherence-ms", type=int, default=2500)
    parser.add_argument("--max-workers", type=int, default=16)
    parser.add_argument("--max-new-events", type=int, default=25)
    parser.add_argument("--max-events-attempted", type=int, default=50)
    parser.add_argument("--target", type=int, default=100)
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--backfill-report", type=Path)
    parser.add_argument("--canary-required", type=int, default=5)
    parser.add_argument("--liquidity-window-report", type=Path)
    parser.add_argument("--max-forecast-lag-minutes", type=int, default=30)
    parser.add_argument("--targeted-forecast-events", type=int, default=0)
    parser.add_argument("--targeted-capture-latency-seconds", type=float, default=30.0)
    parser.add_argument("--targeted-capture-max-buckets", type=int, default=150)
    args = parser.parse_args()
    series = [value.strip().upper() for value in args.series.split(",") if value.strip()]
    unknown = sorted(set(series) - set(SERIES_SYMBOLS))
    if unknown:
        parser.error(f"unsupported series: {','.join(unknown)}")

    rows = []
    # Coherence requires actual fan-out. The normal globally throttled client
    # serializes sibling reads; 429 handling and bounded retries remain active.
    with KalshiClient(throttle_seconds=0.0) as client, session_scope() as session:
        backfill = None
        if args.backfill_report:
            refresh = json.loads(args.backfill_report.read_text(encoding="utf-8"))
            watermark = refresh.get("generated_at")
            if not watermark:
                parser.error("backfill report has no generated_at watermark")
            backfill = backfill_registry(
                session,
                client,
                source_watermark=datetime.fromisoformat(
                    str(watermark).replace("Z", "+00:00")
                ),
                series=series,
            )
            session.commit()
        candidates = [] if args.status_only else discover_candidates_from_registry(session, series)
        if candidates and args.liquidity_window_report and args.liquidity_window_report.exists():
            window_policy = json.loads(
                args.liquidity_window_report.read_text(encoding="utf-8")
            )
            candidates = select_candidates_for_liquidity_window(
                candidates,
                window_policy,
                now=datetime.now().astimezone(),
                fallback_when_empty=args.targeted_forecast_events <= 0,
            )
        targeted_forecasts = refresh_targeted_event_forecasts(
            session,
            client,
            candidates,
            max_events=args.targeted_forecast_events,
            capture_immediately=args.targeted_forecast_events > 0,
            capture_latency_budget_seconds=args.targeted_capture_latency_seconds,
            capture_coherence_ms=args.coherence_ms,
            capture_max_workers=args.max_workers,
            capture_bucket_request_budget=args.targeted_capture_max_buckets,
        )
        session.commit()
        immediately_attempted = {
            str(row["event_ticker"])
            for row in targeted_forecasts.get("rows", [])
            if row.get("immediate_capture", {}).get("attempted")
        }
        candidates = select_candidates_with_fresh_forecasts(
            session,
            candidates,
            now=datetime.now().astimezone(),
            max_lag_minutes=args.max_forecast_lag_minutes,
        )
        starting_status = cohort_status(session, target=args.target)
        canary_remaining = max(
            0, args.canary_required - starting_status["complete_vector_events"]
        )
        effective_max_new = (
            min(args.max_new_events, canary_remaining)
            if canary_remaining
            else args.max_new_events
        )
        captured_count = 0
        for candidate in candidates:
            if candidate.event_ticker in immediately_attempted:
                continue
            if (
                captured_count >= effective_max_new
                or len(rows) >= args.max_events_attempted
            ):
                break
            capture, reasons = capture_candidate(
                session,
                client,
                candidate,
                coherence_limit_ms=args.coherence_ms,
                max_workers=args.max_workers,
            )
            rows.append(
                {
                    "event_ticker": candidate.event_ticker,
                    "status": "CAPTURED" if capture is not None else "SKIPPED",
                    "capture_id": capture.id if capture is not None else None,
                    "reasons": reasons,
                }
            )
            session.commit()
            if capture is not None:
                captured_count += 1
        status = cohort_status(session, target=args.target)
    payload = {
        "policy": "ATOMIC_EVENT_COHERENT_COMPLETE_VECTORS",
        "generated_at": datetime.now(UTC).isoformat(),
        **status,
        "canary_required": args.canary_required,
        "canary_passed": status["complete_vector_events"] >= args.canary_required,
        "accumulation_enabled": status["complete_vector_events"] >= args.canary_required,
        "registry_backfill": backfill,
        "targeted_forecast_refresh": targeted_forecasts,
        "events": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "events"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
