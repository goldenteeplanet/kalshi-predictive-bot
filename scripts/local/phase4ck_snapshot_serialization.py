"""Measure deterministic compact snapshot serialization without connected I/O."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ck.snapshot-serialization-input.v1"
REPORT_SCHEMA = "phase4ck.snapshot-serialization-report.v1"
MAX_SNAPSHOTS = 10_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "snapshots", "artifact_hash"}:
        raise ValueError("PHASE4CK_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CK_INPUT_SCHEMA_OR_HASH_INVALID")
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots or len(snapshots) > MAX_SNAPSHOTS:
        raise ValueError("PHASE4CK_SNAPSHOT_COUNT_INVALID")

    identifiers: set[str] = set()
    normalized = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "snapshot_id",
            "market_ticker",
            "sequence",
            "yes_bids",
            "no_bids",
        }:
            raise ValueError("PHASE4CK_SNAPSHOT_FIELDS_INVALID")
        identifier = snapshot["snapshot_id"]
        ticker = snapshot["market_ticker"]
        sequence = snapshot["sequence"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4CK_SNAPSHOT_ID_INVALID")
        if not isinstance(ticker, str) or not ticker:
            raise ValueError("PHASE4CK_TICKER_INVALID")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("PHASE4CK_SEQUENCE_INVALID")
        identifiers.add(identifier)
        for side in ("yes_bids", "no_bids"):
            levels = snapshot[side]
            if not isinstance(levels, list):
                raise ValueError("PHASE4CK_LEVELS_INVALID")
            for level in levels:
                if (
                    not isinstance(level, list)
                    or len(level) != 2
                    or any(isinstance(value, bool) or not isinstance(value, int) for value in level)
                    or not 0 <= level[0] <= 100
                    or level[1] <= 0
                ):
                    raise ValueError("PHASE4CK_LEVEL_INVALID")
        normalized.append(snapshot)

    baseline = json.dumps(normalized, sort_keys=True, indent=2, ensure_ascii=False).encode()
    compact = _canonical_bytes(normalized)
    round_trip = json.loads(compact)
    if round_trip != normalized or canonical_hash(round_trip) != canonical_hash(normalized):
        raise ValueError("PHASE4CK_SEMANTIC_IDENTITY_FAILED")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CK",
        "input_hash": payload["artifact_hash"],
        "snapshot_count": len(normalized),
        "semantic_hash": canonical_hash(normalized),
        "baseline_bytes": len(baseline),
        "compact_bytes": len(compact),
        "bytes_saved": len(baseline) - len(compact),
        "round_trip_equal": True,
        "optimization_applied_to_runtime": False,
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
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.snapshots.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
