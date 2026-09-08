"""Audit timestamp skew against explicit related-market coherence windows."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ce.snapshot-coherence-input.v1"
REPORT_SCHEMA = "phase4ce.snapshot-coherence-audit.v1"
MAX_WINDOW_MS = 3_600_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("PHASE4CE_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4CE_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("PHASE4CE_TIMESTAMP_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def build_audit(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CE_INPUT_SCHEMA_OR_HASH_INVALID")
    groups, snapshots = payload.get("groups"), payload.get("snapshots")
    if not isinstance(groups, list) or not groups or not isinstance(snapshots, list):
        raise ValueError("PHASE4CE_GROUPS_OR_SNAPSHOTS_INVALID")
    snapshot_index: dict[str, datetime] = {}
    for row in snapshots:
        if not isinstance(row, dict) or set(row) != {"market_id", "captured_at"}:
            raise ValueError("PHASE4CE_SNAPSHOT_FIELDS_INVALID")
        market_id = row["market_id"]
        if not isinstance(market_id, str) or not market_id or market_id in snapshot_index:
            raise ValueError("PHASE4CE_SNAPSHOT_IDENTITY_INVALID")
        snapshot_index[market_id] = _time(row["captured_at"])
    group_names: set[str] = set()
    referenced: set[str] = set()
    results = []
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"group_id", "market_ids", "window_ms"}:
            raise ValueError("PHASE4CE_GROUP_FIELDS_INVALID")
        group_id, market_ids, window_ms = group["group_id"], group["market_ids"], group["window_ms"]
        if not isinstance(group_id, str) or not group_id or group_id in group_names:
            raise ValueError("PHASE4CE_GROUP_IDENTITY_INVALID")
        group_names.add(group_id)
        if not isinstance(market_ids, list) or len(market_ids) < 2:
            raise ValueError("PHASE4CE_GROUP_MARKETS_INVALID")
        if market_ids != sorted(market_ids) or len(market_ids) != len(set(market_ids)):
            raise ValueError("PHASE4CE_GROUP_MARKET_ORDER_OR_DUPLICATE_INVALID")
        if any(market_id not in snapshot_index for market_id in market_ids):
            raise ValueError("PHASE4CE_GROUP_SNAPSHOT_MISSING")
        if (
            isinstance(window_ms, bool)
            or not isinstance(window_ms, int)
            or not 0 <= window_ms <= MAX_WINDOW_MS
        ):
            raise ValueError("PHASE4CE_WINDOW_INVALID")
        referenced.update(market_ids)
        times = [snapshot_index[market_id] for market_id in market_ids]
        skew = max(times) - min(times)
        skew_ms = skew.days * 86_400_000 + skew.seconds * 1_000 + skew.microseconds // 1_000
        coherent = skew_ms <= window_ms
        results.append(
            {
                "group_id": group_id,
                "market_ids": market_ids,
                "window_ms": window_ms,
                "observed_skew_ms": skew_ms,
                "coherent": coherent,
                "offline_evaluation_eligible": coherent,
            }
        )
    ungrouped = sorted(set(snapshot_index) - referenced)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CE",
        "input_hash": payload["artifact_hash"],
        "groups": results,
        "all_groups_coherent": all(row["coherent"] for row in results),
        "incoherent_group_ids": [row["group_id"] for row in results if not row["coherent"]],
        "ungrouped_market_ids": ungrouped,
        "offline_only": True,
        "production_records_created": 0,
        "execution_authorized": False,
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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_audit(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
