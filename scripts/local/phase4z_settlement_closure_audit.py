"""Read-only Phase 4Z audit of the settlement-hint closure path."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.ingest.settlement_hints import (
    ALLOWED_HINT_FIELDS,
    SCHEMA,
    artifact_hash,
)


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=1")
    return connection


def _rows_hash(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _load_hints(path: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError("SETTLEMENT_HINT_SCHEMA_INVALID")
    if payload.get("artifact_hash") != artifact_hash(payload):
        raise ValueError("SETTLEMENT_HINT_HASH_MISMATCH")
    hints = payload.get("hints")
    if not isinstance(hints, list):
        raise ValueError("SETTLEMENT_HINT_ROWS_INVALID")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for hint in hints:
        if not isinstance(hint, dict) or set(hint) != ALLOWED_HINT_FIELDS:
            raise ValueError("SETTLEMENT_HINT_FIELDS_INVALID")
        ticker = str(hint["ticker"])
        if ticker in seen:
            raise ValueError("SETTLEMENT_HINT_DUPLICATE_TICKER")
        seen.add(ticker)
        normalized.append({key: str(hint[key]) for key in sorted(ALLOWED_HINT_FIELDS)})
    return payload, normalized


def audit(
    source_db: Path,
    research_db: Path,
    hint_artifact: Path,
    *,
    now: datetime,
) -> dict[str, Any]:
    payload, hints = _load_hints(hint_artifact)
    source = _ro(source_db)
    research = _ro(research_db)
    captures_by_ticker: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in research.execute(
        """
        SELECT capture_id,ticker,settlement_target,bundle_hash
        FROM prospective_paired_captures
        ORDER BY ticker,capture_id
        """
    ):
        captures_by_ticker[row["ticker"]].append(row)
    evaluated = {
        row[0]
        for row in research.execute("SELECT capture_id FROM prospective_pair_evaluations")
    }
    rows: list[dict[str, Any]] = []
    for hint in hints:
        ticker = hint["ticker"]
        captures = captures_by_ticker.get(ticker, [])
        capture_ids = [str(row["capture_id"]) for row in captures]
        evaluated_count = sum(capture_id in evaluated for capture_id in capture_ids)
        settlement = source.execute(
            """
            SELECT ticker,settled_at,result,yes_settlement_value,updated_at
            FROM settlements WHERE ticker=?
            """,
            (ticker,),
        ).fetchone()
        canonical = settlement is not None and bool(settlement["result"])
        if not captures:
            status = "HINT_NO_CAPTURE"
        elif evaluated_count == len(captures):
            status = "EVALUATED"
        elif evaluated_count:
            status = "PARTIAL_EVALUATION"
        elif canonical:
            status = "CANONICAL_PRESENT_AWAITING_EVALUATION"
        else:
            status = "UNRESOLVED_AFTER_HINT"
        rows.append(
            {
                "ticker": ticker,
                "due_at": hint["due_at"],
                "source_capture_id_hash": hint["source_capture_id_hash"],
                "bundle_set_hash": hint["bundle_set_hash"],
                "capture_count": len(captures),
                "evaluated_count": evaluated_count,
                "canonical_present": canonical,
                "settled_at": None if settlement is None else settlement["settled_at"],
                "settlement_updated_at": (
                    None if settlement is None else settlement["updated_at"]
                ),
                "status": status,
            }
        )
    source.close()
    research.close()
    counts = Counter(row["status"] for row in rows)
    return {
        "schema": "phase4z.settlement-closure-audit.v1",
        "generated_at": now.astimezone(UTC).isoformat(),
        "source_mode": "ro/query_only",
        "research_mode": "ro/query_only",
        "hint_schema": payload["schema"],
        "hint_generated_at": payload["generated_at"],
        "hint_artifact_hash": payload["artifact_hash"],
        "hint_count": len(hints),
        "status_counts": dict(sorted(counts.items())),
        "canonical_count": sum(bool(row["canonical_present"]) for row in rows),
        "fully_evaluated_count": counts["EVALUATED"],
        "closure_complete": bool(rows) and counts["EVALUATED"] == len(rows),
        "rows_hash": _rows_hash(rows),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--hint-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    result = audit(
        args.source_db,
        args.research_db,
        args.hint_artifact,
        now=datetime.now(UTC),
    )
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
