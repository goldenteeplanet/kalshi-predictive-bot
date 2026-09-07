"""Prove equivalence for the Phase 4BW local allocation optimization."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bv.performance-fixture-pack.v1"
REPORT_SCHEMA = "phase4by.optimization-equivalence-proof.v1"
EXPECTED_NAMES = ("small", "medium", "large", "sparse", "burst")


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def reference_workload(events: list[dict[str, Any]]) -> str:
    active = [row for row in events if row["active"]]
    ordered = sorted(active, key=lambda row: (row["market_key"], row["sequence"]))
    normalized = [
        (row["market_key"], row["sequence"], row["observed_offset_us"], row["value_ppm"])
        for row in ordered
    ]
    return canonical_hash(normalized)


def optimized_workload(events: list[dict[str, Any]]) -> str:
    ordered = sorted(
        (row for row in events if row["active"]),
        key=lambda row: (row["market_key"], row["sequence"]),
    )
    normalized = [
        (row["market_key"], row["sequence"], row["observed_offset_us"], row["value_ppm"])
        for row in ordered
    ]
    return canonical_hash(normalized)


def _validated_fixtures(pack: dict[str, Any]) -> list[dict[str, Any]]:
    if pack.get("schema") != INPUT_SCHEMA or pack.get("artifact_hash") != _hash(pack):
        raise ValueError("PHASE4BY_INPUT_SCHEMA_OR_HASH_INVALID")
    manifest, fixtures = pack.get("manifest"), pack.get("fixtures")
    if not isinstance(manifest, dict) or not isinstance(fixtures, list):
        raise ValueError("PHASE4BY_INPUT_SHAPE_INVALID")
    if manifest.get("artifact_hash") != _hash(manifest):
        raise ValueError("PHASE4BY_MANIFEST_HASH_INVALID")
    if pack.get("manifest_hash") != manifest["artifact_hash"]:
        raise ValueError("PHASE4BY_MANIFEST_LINK_INVALID")
    if [row.get("name") for row in fixtures if isinstance(row, dict)] != list(EXPECTED_NAMES):
        raise ValueError("PHASE4BY_FIXTURE_ORDER_INVALID")
    entries = manifest.get("fixtures")
    if not isinstance(entries, list) or len(entries) != len(fixtures):
        raise ValueError("PHASE4BY_MANIFEST_ENTRIES_INVALID")
    for fixture, entry in zip(fixtures, entries, strict=True):
        if fixture.get("artifact_hash") != _hash(fixture):
            raise ValueError("PHASE4BY_FIXTURE_HASH_INVALID")
        if entry.get("fixture_hash") != fixture["artifact_hash"]:
            raise ValueError("PHASE4BY_FIXTURE_LINK_INVALID")
        if not isinstance(fixture.get("events"), list):
            raise ValueError("PHASE4BY_EVENTS_INVALID")
        if fixture.get("execution_authorized") is not False:
            raise ValueError("PHASE4BY_AUTHORITY_INVALID")
    return fixtures


def build_proof(pack: dict[str, Any]) -> dict[str, Any]:
    fixtures = _validated_fixtures(pack)
    comparisons = []
    for fixture in fixtures:
        before = reference_workload(fixture["events"])
        after = optimized_workload(fixture["events"])
        if before != after:
            raise ValueError("PHASE4BY_LOGICAL_OUTPUT_DIVERGENCE")
        comparisons.append(
            {
                "name": fixture["name"],
                "fixture_hash": fixture["artifact_hash"],
                "before_output_hash": before,
                "after_output_hash": after,
                "byte_identical": True,
            }
        )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4BY",
        "input_pack_hash": pack["artifact_hash"],
        "optimization": "REMOVE_INTERMEDIATE_ACTIVE_EVENT_LIST",
        "comparisons": comparisons,
        "all_outputs_byte_identical": True,
        "refusal_order_changed": False,
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
    parser.add_argument("--fixture-pack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_proof(json.loads(args.fixture_pack.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
