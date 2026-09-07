"""Profile deterministic guarded-pipeline computations against offline fixture artifacts."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bv.performance-fixture-pack.v1"
FIXTURE_SCHEMA = "phase4bv.performance-fixture.v1"
REPORT_SCHEMA = "phase4bw.read-only-profile.v1"
EXPECTED_NAMES = ("small", "medium", "large", "sparse", "burst")
Measurement = Callable[[list[dict[str, Any]]], tuple[int, int, str]]


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _workload(events: list[dict[str, Any]]) -> str:
    ordered = sorted(
        (row for row in events if row["active"]),
        key=lambda row: (row["market_key"], row["sequence"]),
    )
    normalized = [
        (row["market_key"], row["sequence"], row["observed_offset_us"], row["value_ppm"])
        for row in ordered
    ]
    return canonical_hash(normalized)


def _measure(events: list[dict[str, Any]]) -> tuple[int, int, str]:
    tracemalloc.start()
    started = time.perf_counter_ns()
    output_hash = _workload(events)
    elapsed_ns = time.perf_counter_ns() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return elapsed_ns, peak_bytes, output_hash


def _validate_pack(pack: dict[str, Any]) -> list[dict[str, Any]]:
    if pack.get("schema") != INPUT_SCHEMA or pack.get("artifact_hash") != _hash(pack):
        raise ValueError("PHASE4BW_INPUT_SCHEMA_OR_HASH_INVALID")
    fixtures = pack.get("fixtures")
    manifest = pack.get("manifest")
    if not isinstance(fixtures, list) or not isinstance(manifest, dict):
        raise ValueError("PHASE4BW_INPUT_SHAPE_INVALID")
    if manifest.get("artifact_hash") != _hash(manifest):
        raise ValueError("PHASE4BW_MANIFEST_HASH_INVALID")
    if pack.get("manifest_hash") != manifest["artifact_hash"]:
        raise ValueError("PHASE4BW_MANIFEST_LINK_INVALID")
    if [row.get("name") for row in fixtures if isinstance(row, dict)] != list(EXPECTED_NAMES):
        raise ValueError("PHASE4BW_FIXTURE_ORDER_INVALID")
    entries = manifest.get("fixtures")
    if not isinstance(entries, list) or len(entries) != len(fixtures):
        raise ValueError("PHASE4BW_MANIFEST_ENTRIES_INVALID")
    for fixture, entry in zip(fixtures, entries, strict=True):
        if fixture.get("schema") != FIXTURE_SCHEMA or fixture.get("artifact_hash") != _hash(
            fixture
        ):
            raise ValueError("PHASE4BW_FIXTURE_HASH_INVALID")
        events = fixture.get("events")
        envelope = fixture.get("expected_envelope")
        if not isinstance(events, list) or not events or not isinstance(envelope, dict):
            raise ValueError("PHASE4BW_FIXTURE_SHAPE_INVALID")
        if entry.get("fixture_hash") != fixture["artifact_hash"]:
            raise ValueError("PHASE4BW_FIXTURE_LINK_INVALID")
        if fixture.get("execution_authorized") is not False:
            raise ValueError("PHASE4BW_AUTHORITY_INVALID")
    return fixtures


def build_profile(pack: dict[str, Any], *, measure: Measurement = _measure) -> dict[str, Any]:
    fixtures = _validate_pack(pack)
    results = []
    for fixture in fixtures:
        elapsed_ns, peak_bytes, output_hash = measure(fixture["events"])
        invalid_numbers = any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (elapsed_ns, peak_bytes)
        )
        if invalid_numbers:
            raise ValueError("PHASE4BW_MEASUREMENT_INVALID")
        if not isinstance(output_hash, str) or len(output_hash) != 64:
            raise ValueError("PHASE4BW_OUTPUT_HASH_INVALID")
        envelope = fixture["expected_envelope"]
        elapsed_ms = elapsed_ns / 1_000_000
        peak_kib = (peak_bytes + 1_023) // 1_024
        results.append(
            {
                "name": fixture["name"],
                "fixture_hash": fixture["artifact_hash"],
                "event_count": len(fixture["events"]),
                "elapsed_ns": elapsed_ns,
                "peak_bytes": peak_bytes,
                "output_hash": output_hash,
                "elapsed_within_envelope": elapsed_ms <= envelope["max_elapsed_ms"],
                "memory_within_envelope": peak_kib <= envelope["max_peak_kib"],
            }
        )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4BW",
        "input_pack_hash": pack["artifact_hash"],
        "profiles": results,
        "all_envelopes_satisfied": all(
            row["elapsed_within_envelope"] and row["memory_within_envelope"] for row in results
        ),
        "measurement_scope": "OFFLINE_ARTIFACT_ONLY",
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
    pack = json.loads(args.fixture_pack.read_text(encoding="utf-8"))
    report = build_profile(pack)
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
