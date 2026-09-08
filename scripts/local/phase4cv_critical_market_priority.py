"""Prioritize market-data evaluation work without producing trading instructions."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cv.priority-input.v1"
REPORT_SCHEMA = "phase4cv.priority-report.v1"
MAX_MARKETS = 100_000
TIERS = {"CRITICAL": 0, "UPCOMING": 1, "BLOCKED": 2, "EXPIRED": 3}


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4CV_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4CV_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4CV_TIMESTAMP_INVALID")
    return parsed


def _ms(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "evaluated_at", "critical_window_ms", "markets", "artifact_hash"}:
        raise ValueError("PHASE4CV_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CV_INPUT_SCHEMA_OR_HASH_INVALID")
    now = _time(payload["evaluated_at"])
    window = payload.get("critical_window_ms")
    if isinstance(window, bool) or not isinstance(window, int) or window < 0:
        raise ValueError("PHASE4CV_CRITICAL_WINDOW_INVALID")
    markets = payload.get("markets")
    if not isinstance(markets, list) or not markets or len(markets) > MAX_MARKETS:
        raise ValueError("PHASE4CV_MARKET_COUNT_INVALID")

    tickers: set[str] = set()
    rows = []
    for market in markets:
        required = {"ticker", "evaluation_at", "evidence_ready", "data_fresh"}
        if not isinstance(market, dict) or set(market) != required:
            raise ValueError("PHASE4CV_MARKET_FIELDS_INVALID")
        ticker = market["ticker"]
        if not isinstance(ticker, str) or not ticker or ticker in tickers:
            raise ValueError("PHASE4CV_TICKER_INVALID")
        if not isinstance(market["evidence_ready"], bool) or not isinstance(
            market["data_fresh"], bool
        ):
            raise ValueError("PHASE4CV_READINESS_INVALID")
        tickers.add(ticker)
        delta = _ms(_time(market["evaluation_at"]), now)
        ready = market["evidence_ready"] and market["data_fresh"]
        if delta < 0:
            tier, reason = "EXPIRED", "EVALUATION_WINDOW_PASSED"
        elif not ready:
            tier, reason = "BLOCKED", "EVIDENCE_OR_FRESHNESS_NOT_READY"
        elif delta <= window:
            tier, reason = "CRITICAL", "WITHIN_CRITICAL_WINDOW"
        else:
            tier, reason = "UPCOMING", "OUTSIDE_CRITICAL_WINDOW"
        rows.append(
            {
                "ticker": ticker,
                "tier": tier,
                "reason": reason,
                "time_to_evaluation_ms": delta,
                "evaluation_at": market["evaluation_at"],
            }
        )
    rows.sort(
        key=lambda row: (
            TIERS[row["tier"]],
            max(row["time_to_evaluation_ms"], 0),
            row["ticker"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["priority_rank"] = rank
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CV",
        "input_hash": payload["artifact_hash"],
        "markets": rows,
        "critical_count": sum(row["tier"] == "CRITICAL" for row in rows),
        "trading_fields_emitted": [],
        "orders_created": 0,
        "execution_authorized": False,
        "production_records_created": 0,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.markets.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
