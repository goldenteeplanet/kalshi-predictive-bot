"""Publish a fail-closed Phase 4AA status artifact from a Phase 4Z audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "phase4aa.settlement-closure-gate.v1"
SOURCE_SCHEMA = "phase4z.settlement-closure-audit.v1"
BLOCKING_STATUSES = {"HINT_NO_CAPTURE", "PARTIAL_EVALUATION"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def artifact_hash(payload: dict[str, Any]) -> str:
    return _hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def load_closure(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SOURCE_SCHEMA:
        raise ValueError("PHASE4AA_SOURCE_SCHEMA_INVALID")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("PHASE4AA_SOURCE_ROWS_MISSING")
    if payload.get("rows_hash") != _hash(rows):
        raise ValueError("PHASE4AA_SOURCE_ROWS_HASH_MISMATCH")
    expected_counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("status"), str):
            raise ValueError("PHASE4AA_SOURCE_ROW_INVALID")
        status = row["status"]
        expected_counts[status] = expected_counts.get(status, 0) + 1
    if payload.get("status_counts") != dict(sorted(expected_counts.items())):
        raise ValueError("PHASE4AA_SOURCE_COUNTS_MISMATCH")
    if payload.get("hint_count") != len(rows):
        raise ValueError("PHASE4AA_SOURCE_HINT_COUNT_MISMATCH")
    return payload


def build_status(source: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    counts = source["status_counts"]
    hint_count = int(source["hint_count"])
    blocking = {key: int(counts.get(key, 0)) for key in sorted(BLOCKING_STATUSES)}
    blocking = {key: value for key, value in blocking.items() if value}
    if hint_count == 0:
        state = "WAITING_NO_HINTS"
        reason = "NO_DUE_CAPTURE_HINTS"
    elif blocking:
        state = "ATTENTION"
        reason = "CLOSURE_LINEAGE_INCOMPLETE"
    elif bool(source.get("closure_complete")):
        state = "COMPLETE"
        reason = "ALL_HINTED_TICKERS_EVALUATED"
    elif int(counts.get("CANONICAL_PRESENT_AWAITING_EVALUATION", 0)):
        state = "WAITING_RECONCILIATION"
        reason = "CANONICAL_SETTLEMENTS_AWAIT_EVALUATION"
    else:
        state = "WAITING_SETTLEMENT"
        reason = "EXACT_HINTS_REMAIN_UNRESOLVED"
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": now.astimezone(UTC).isoformat(),
        "state": state,
        "reason": reason,
        "safe_to_advance": state == "COMPLETE",
        "trading_mode_changed": False,
        "production_database_written": False,
        "source_schema": source["schema"],
        "source_generated_at": source["generated_at"],
        "source_rows_hash": source["rows_hash"],
        "source_hint_artifact_hash": source["hint_artifact_hash"],
        "hint_count": hint_count,
        "canonical_count": int(source["canonical_count"]),
        "fully_evaluated_count": int(source["fully_evaluated_count"]),
        "status_counts": counts,
        "blocking_counts": blocking,
    }
    payload["artifact_hash"] = artifact_hash(payload)
    return payload


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--closure-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_status(load_closure(args.closure_audit), now=datetime.now(UTC))
    write_atomic(args.output, payload)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
