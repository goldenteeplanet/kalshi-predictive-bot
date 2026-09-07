"""Build a deterministic in-memory index for unchanged order-book snapshots."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cd.snapshot-deduplication-input.v1"
REPORT_SCHEMA = "phase4cd.snapshot-deduplication-result.v1"
SIDES = ("yes", "no")


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _book(book: Any) -> dict[str, list[list[int]]]:
    if not isinstance(book, dict) or set(book) != set(SIDES):
        raise ValueError("PHASE4CD_ORDER_BOOK_FIELDS_INVALID")
    normalized: dict[str, list[list[int]]] = {}
    for side in SIDES:
        levels = book[side]
        if not isinstance(levels, list):
            raise ValueError("PHASE4CD_ORDER_BOOK_LEVELS_INVALID")
        seen_prices: set[int] = set()
        clean = []
        for level in levels:
            if not isinstance(level, list) or len(level) != 2:
                raise ValueError("PHASE4CD_ORDER_BOOK_LEVEL_INVALID")
            price, quantity = level
            invalid = any(isinstance(value, bool) or not isinstance(value, int) for value in level)
            if invalid or not 0 <= price <= 100 or quantity <= 0 or price in seen_prices:
                raise ValueError("PHASE4CD_ORDER_BOOK_VALUE_INVALID")
            seen_prices.add(price)
            clean.append([price, quantity])
        normalized[side] = sorted(clean, key=lambda row: row[0])
    return normalized


def build_index(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CD_INPUT_SCHEMA_OR_HASH_INVALID")
    prior, snapshots = payload.get("prior_index"), payload.get("snapshots")
    if not isinstance(prior, list) or not isinstance(snapshots, list) or not snapshots:
        raise ValueError("PHASE4CD_PRIOR_OR_SNAPSHOTS_INVALID")
    index: dict[str, dict[str, Any]] = {}
    for row in prior:
        if not isinstance(row, dict) or set(row) != {"market_id", "sequence", "content_hash"}:
            raise ValueError("PHASE4CD_PRIOR_FIELDS_INVALID")
        market_id = row["market_id"]
        if not isinstance(market_id, str) or not market_id or market_id in index:
            raise ValueError("PHASE4CD_PRIOR_IDENTITY_INVALID")
        if (
            isinstance(row["sequence"], bool)
            or not isinstance(row["sequence"], int)
            or row["sequence"] < 0
        ):
            raise ValueError("PHASE4CD_PRIOR_SEQUENCE_INVALID")
        if not isinstance(row["content_hash"], str) or len(row["content_hash"]) != 64:
            raise ValueError("PHASE4CD_PRIOR_HASH_INVALID")
        index[market_id] = dict(row)
    decisions = []
    hash_payloads: dict[str, str] = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "market_id",
            "sequence",
            "order_book",
        }:
            raise ValueError("PHASE4CD_SNAPSHOT_FIELDS_INVALID")
        market_id, sequence = snapshot["market_id"], snapshot["sequence"]
        if not isinstance(market_id, str) or not market_id:
            raise ValueError("PHASE4CD_MARKET_ID_INVALID")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("PHASE4CD_SEQUENCE_INVALID")
        previous = index.get(market_id)
        if previous is not None and sequence <= previous["sequence"]:
            raise ValueError("PHASE4CD_SEQUENCE_REGRESSION_OR_DUPLICATE")
        normalized = _book(snapshot["order_book"])
        serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        content_hash = canonical_hash(normalized)
        collided = hash_payloads.get(content_hash)
        if collided is not None and collided != serialized:
            raise ValueError("PHASE4CD_CONTENT_HASH_COLLISION")
        hash_payloads[content_hash] = serialized
        unchanged = previous is not None and previous["content_hash"] == content_hash
        decisions.append(
            {
                "market_id": market_id,
                "sequence": sequence,
                "content_hash": content_hash,
                "decision": "SKIP_UNCHANGED" if unchanged else "RECOMPUTE",
            }
        )
        index[market_id] = {
            "market_id": market_id,
            "sequence": sequence,
            "content_hash": content_hash,
        }
    result: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CD",
        "input_hash": payload["artifact_hash"],
        "decisions": decisions,
        "resulting_index": [index[key] for key in sorted(index)],
        "recompute_count": sum(row["decision"] == "RECOMPUTE" for row in decisions),
        "skipped_unchanged_count": sum(row["decision"] == "SKIP_UNCHANGED" for row in decisions),
        "index_scope": "IN_MEMORY_ARTIFACT_ONLY",
        "production_records_created": 0,
        "execution_authorized": False,
    }
    result["artifact_hash"] = _hash(result)
    return result


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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_index(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
