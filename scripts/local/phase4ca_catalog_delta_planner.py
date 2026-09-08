"""Plan an artifact-only incremental market catalog refresh."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ca.catalog-state.v1"
PLAN_SCHEMA = "phase4ca.catalog-delta-plan.v1"
MAX_INCREMENTAL_CYCLES = 24


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _index(rows: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError(f"PHASE4CA_{label}_MISSING")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"market_id", "revision", "content_hash"}:
            raise ValueError(f"PHASE4CA_{label}_FIELDS_INVALID")
        market_id, revision, content_hash = row["market_id"], row["revision"], row["content_hash"]
        if not isinstance(market_id, str) or not market_id or market_id in result:
            raise ValueError(f"PHASE4CA_{label}_IDENTITY_INVALID")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError(f"PHASE4CA_{label}_REVISION_INVALID")
        if not isinstance(content_hash, str) or len(content_hash) != 64:
            raise ValueError(f"PHASE4CA_{label}_HASH_INVALID")
        result[market_id] = row
    return result


def build_plan(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("schema") != INPUT_SCHEMA or state.get("artifact_hash") != _hash(state):
        raise ValueError("PHASE4CA_INPUT_SCHEMA_OR_HASH_INVALID")
    cycle = state.get("incremental_cycles_since_full")
    force_full = state.get("force_full_reconciliation")
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 0:
        raise ValueError("PHASE4CA_CYCLE_INVALID")
    if not isinstance(force_full, bool):
        raise ValueError("PHASE4CA_FORCE_FULL_INVALID")
    previous = _index(state.get("previous_catalog"), "PREVIOUS_CATALOG")
    discovery = _index(state.get("discovered_catalog"), "DISCOVERED_CATALOG")
    full = force_full or cycle >= MAX_INCREMENTAL_CYCLES or not previous
    previous_ids, discovered_ids = set(previous), set(discovery)
    if full:
        fetch = sorted(discovered_ids)
        unchanged: list[str] = []
    else:
        unchanged = sorted(
            market_id
            for market_id in previous_ids & discovered_ids
            if previous[market_id]["revision"] == discovery[market_id]["revision"]
            and previous[market_id]["content_hash"] == discovery[market_id]["content_hash"]
        )
        fetch = sorted(discovered_ids - set(unchanged))
    removed = sorted(previous_ids - discovered_ids)
    reason = (
        "FORCED_FULL"
        if force_full
        else "PERIODIC_FULL"
        if cycle >= MAX_INCREMENTAL_CYCLES
        else "INITIAL_FULL"
        if not previous
        else "INCREMENTAL_DELTA"
    )
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "phase": "4CA",
        "input_hash": state["artifact_hash"],
        "mode": "FULL_RECONCILIATION" if full else "INCREMENTAL",
        "reason": reason,
        "fetch_market_ids": fetch,
        "unchanged_market_ids": unchanged,
        "removed_market_ids": removed,
        "fetch_count": len(fetch),
        "avoided_refetch_count": len(unchanged),
        "next_incremental_cycles_since_full": 0 if full else cycle + 1,
        "full_reconciliation_preserved": True,
        "artifact_only": True,
        "production_records_created": 0,
        "execution_authorized": False,
    }
    plan["artifact_hash"] = _hash(plan)
    return plan


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
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = build_plan(json.loads(args.state.read_text(encoding="utf-8")))
    publish(args.output, plan)
    print(json.dumps(plan, sort_keys=True))


if __name__ == "__main__":
    main()
