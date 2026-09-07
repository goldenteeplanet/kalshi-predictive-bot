"""Read-only Phase 4X attribution for prospective settlement lag."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=1")
    return connection


def _utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _hash(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def audit(source_db: Path, research_db: Path, *, now: datetime) -> dict[str, Any]:
    source = _ro(source_db)
    research = _ro(research_db)
    evaluated = {
        row[0] for row in research.execute("SELECT capture_id FROM prospective_pair_evaluations")
    }
    captures = research.execute(
        """
        SELECT capture_id,ticker,event_ticker,series_ticker,snapshot_timestamp,
               settlement_target,bundle_hash
        FROM prospective_paired_captures ORDER BY ticker,snapshot_timestamp,capture_id
        """
    ).fetchall()
    market_sql = """
        SELECT ticker,status,result,close_time,expected_expiration_time,expiration_time,
               settlement_ts,last_seen_at FROM markets WHERE ticker=?
    """
    settlement_sql = """
        SELECT ticker,settled_at,result,yes_settlement_value,updated_at,raw_json
        FROM settlements WHERE ticker=?
    """
    rows: list[dict[str, Any]] = []
    for capture in captures:
        if capture["capture_id"] in evaluated:
            continue
        market = source.execute(market_sql, (capture["ticker"],)).fetchone()
        settlement = source.execute(settlement_sql, (capture["ticker"],)).fetchone()
        target = _utc(capture["settlement_target"])
        due = target is not None and target <= now
        close_time = None if market is None else _utc(market["close_time"])
        expiration_time = None if market is None else _utc(market["expiration_time"])
        exchange_due_at = close_time or expiration_time
        exchange_due = exchange_due_at is not None and exchange_due_at <= now
        market_result = None if market is None else market["result"]
        canonical = settlement is not None and bool(settlement["result"])
        if target is None:
            reason = "SETTLEMENT_TARGET_MISSING"
        elif not due:
            reason = "NOT_DUE"
        elif canonical:
            reason = "DUE_CANONICAL_PRESENT_AWAITING_RECONCILE"
        elif exchange_due_at is not None and not exchange_due:
            reason = "TARGET_ELAPSED_EXCHANGE_NOT_DUE"
        elif market_result and str(market_result).lower() not in {"", "unknown"}:
            reason = "DUE_MARKET_RESULT_WITHOUT_CANONICAL"
        elif market is None:
            reason = "DUE_MARKET_ROW_MISSING"
        else:
            reason = "DUE_API_UNRESOLVED"
        rows.append(
            {
                "capture_id": capture["capture_id"],
                "ticker": capture["ticker"],
                "event_ticker": capture["event_ticker"],
                "series_ticker": capture["series_ticker"],
                "snapshot_timestamp": capture["snapshot_timestamp"],
                "settlement_target": capture["settlement_target"],
                "bundle_hash": capture["bundle_hash"],
                "reason": reason,
                "market_status": None if market is None else market["status"],
                "market_result": market_result,
                "close_time": None if market is None else market["close_time"],
                "exchange_due_at": None if exchange_due_at is None else exchange_due_at.isoformat(),
                "expected_expiration_time": None
                if market is None
                else market["expected_expiration_time"],
                "expiration_time": None if market is None else market["expiration_time"],
                "market_settlement_ts": None if market is None else market["settlement_ts"],
                "last_authorized_refresh": None if market is None else market["last_seen_at"],
                "canonical_settled_at": None if settlement is None else settlement["settled_at"],
                "canonical_result": None if settlement is None else settlement["result"],
                "canonical_updated_at": None if settlement is None else settlement["updated_at"],
            }
        )
    reason_counts = Counter(row["reason"] for row in rows)
    status_counts = Counter(
        str(row["market_status"] or "MISSING") for row in rows if row["reason"].startswith("DUE_")
    )
    result = {
        "schema": "phase4x.settlement-lag-audit.v1",
        "generated_at": now.isoformat(),
        "source_mode": "ro/query_only",
        "research_mode": "ro/query_only",
        "unevaluated_rows": len(rows),
        "unevaluated_tickers": len({row["ticker"] for row in rows}),
        "unevaluated_events": len({row["event_ticker"] for row in rows}),
        "reason_counts": dict(sorted(reason_counts.items())),
        "due_status_counts": dict(sorted(status_counts.items())),
        "due_examples": [row for row in rows if row["reason"].startswith("DUE_")][:8],
        "rows_hash": _hash(rows),
        "rows": rows,
    }
    source.close()
    research.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    result = audit(args.source_db, args.research_db, now=datetime.now(UTC))
    rendered_result = {key: value for key, value in result.items() if key != "rows"}
    rendered = json.dumps(
        rendered_result if args.summary_only else result, indent=2, sort_keys=True
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
