#!/usr/bin/env python3
"""Materialize exact point-in-time forecast-to-polytope alignment manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.research.probability_polytope import polytope_information


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-name", default="crypto_v2")
    parser.add_argument("--max-lag-minutes", type=int, default=30)
    parser.add_argument("--max-coherence-ms", type=int, default=2500)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    rows = list(
        connection.execute(
            "SELECT * FROM crypto_event_liquidity_coverage ORDER BY captured_at DESC,id DESC"
        )
    )
    audits = []
    aligned_by_event: dict[str, dict[str, Any]] = {}
    for row in rows:
        forecast = connection.execute(
            """SELECT f.id,f.ticker,f.model_name,f.forecasted_at,f.yes_probability,
                      f.market_mid_probability,f.feature_json
               FROM forecasts f JOIN markets m ON m.ticker=f.ticker
               WHERE m.event_ticker=? AND f.model_name=? AND f.forecasted_at<=?
               ORDER BY f.forecasted_at DESC,f.id DESC LIMIT 1""",
            (row["event_ticker"], args.model_name, row["captured_at"]),
        ).fetchone()
        audit = build_alignment(
            dict(row),
            dict(forecast) if forecast is not None else None,
            max_lag_minutes=args.max_lag_minutes,
            max_coherence_ms=args.max_coherence_ms,
        )
        audits.append(audit)
        if audit["aligned"]:
            aligned_by_event.setdefault(row["event_ticker"], audit)
    connection.close()
    aligned = sorted(aligned_by_event.values(), key=lambda row: row["capture_timestamp"])
    payload = {
        "policy": "EXACT_COHERENT_CAPTURE_WITH_BOUNDED_POINT_IN_TIME_FORECAST",
        "model_name": args.model_name,
        "max_forecast_lag_minutes": args.max_lag_minutes,
        "max_capture_coherence_ms": args.max_coherence_ms,
        "coverage_rows_reviewed": len(rows),
        "unique_aligned_events": len(aligned),
        "unique_events_reviewed": len({row["event_ticker"] for row in rows}),
        "rejection_reason_counts": _reason_counts(audits),
        "aligned_events": aligned,
        "alignment_audit_rows": audits,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary = {
        key: value
        for key, value in payload.items()
        if key not in {"aligned_events", "alignment_audit_rows"}
    }
    print(json.dumps(summary))
    return 0


def build_alignment(
    coverage: dict[str, Any],
    forecast: dict[str, Any] | None,
    *,
    max_lag_minutes: int,
    max_coherence_ms: int,
) -> dict[str, Any]:
    bounds = json.loads(coverage.get("bounds_json") or "{}").get("buckets", [])
    information = polytope_information(bounds)
    capture_time = _timestamp(coverage.get("captured_at"))
    forecast_time = _timestamp(forecast.get("forecasted_at")) if forecast else None
    lag_seconds = (
        (capture_time - forecast_time).total_seconds()
        if capture_time is not None and forecast_time is not None
        else None
    )
    reasons = []
    if forecast is None:
        reasons.append("POINT_IN_TIME_MODEL_FORECAST_MISSING")
    if lag_seconds is None or lag_seconds < 0 or lag_seconds > max_lag_minutes * 60:
        reasons.append("FORECAST_CAPTURE_LAG_EXCEEDED")
    if int(coverage.get("coherence_ms") or 0) > max_coherence_ms:
        reasons.append("CAPTURE_COHERENCE_EXCEEDED")
    if not information["gate_passed"]:
        reasons.append("POLYTOPE_INFORMATION_GATE_FAILED")
    canonical_bounds = json.dumps(bounds, sort_keys=True, separators=(",", ":"))
    feature_json = json.loads(forecast.get("feature_json") or "{}") if forecast else {}
    return {
        "event_ticker": coverage.get("event_ticker"),
        "coverage_id": coverage.get("id"),
        "family": coverage.get("family"),
        "capture_timestamp": coverage.get("captured_at"),
        "capture_coherence_ms": coverage.get("coherence_ms"),
        "bounds_sha256": hashlib.sha256(canonical_bounds.encode()).hexdigest(),
        "bounds": bounds,
        "polytope_information": information,
        "forecast_id": forecast.get("id") if forecast else None,
        "forecast_ticker": forecast.get("ticker") if forecast else None,
        "model_name": forecast.get("model_name") if forecast else None,
        "forecast_timestamp": forecast.get("forecasted_at") if forecast else None,
        "forecast_capture_lag_seconds": lag_seconds,
        "yes_probability": forecast.get("yes_probability") if forecast else None,
        "market_mid_probability": forecast.get("market_mid_probability") if forecast else None,
        "crypto_feature_id": feature_json.get("crypto_feature_id"),
        "aligned": not reasons,
        "rejection_reasons": sorted(set(reasons)),
    }


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except ValueError:
        return None


def _reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for reason in row["rejection_reasons"]:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
