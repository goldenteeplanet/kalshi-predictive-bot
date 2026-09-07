"""Build and validate a deterministic synthetic performance fixture pack."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

FIXTURE_SCHEMA = "phase4bv.performance-fixture.v1"
MANIFEST_SCHEMA = "phase4bv.performance-fixture-manifest.v1"
PACK_SCHEMA = "phase4bv.performance-fixture-pack.v1"
FIXTURE_NAMES = ("small", "medium", "large", "sparse", "burst")
SPECIFICATIONS = {
    "small": {"event_count": 16, "active_ratio_ppm": 750_000, "burst_width": 1},
    "medium": {"event_count": 128, "active_ratio_ppm": 625_000, "burst_width": 4},
    "large": {"event_count": 1_024, "active_ratio_ppm": 500_000, "burst_width": 16},
    "sparse": {"event_count": 256, "active_ratio_ppm": 62_500, "burst_width": 1},
    "burst": {"event_count": 512, "active_ratio_ppm": 875_000, "burst_width": 64},
}
ENVELOPES = {
    "small": {"max_elapsed_ms": 50, "max_peak_kib": 512},
    "medium": {"max_elapsed_ms": 150, "max_peak_kib": 2_048},
    "large": {"max_elapsed_ms": 750, "max_peak_kib": 16_384},
    "sparse": {"max_elapsed_ms": 200, "max_peak_kib": 4_096},
    "burst": {"max_elapsed_ms": 400, "max_peak_kib": 8_192},
}


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _events(name: str, specification: dict[str, int]) -> list[dict[str, Any]]:
    count = specification["event_count"]
    active_count = count * specification["active_ratio_ppm"] // 1_000_000
    width = specification["burst_width"]
    rows = []
    for index in range(count):
        cycle_position = index % max(width * 2, 1)
        active = index < active_count if name != "burst" else cycle_position < width
        rows.append(
            {
                "sequence": index,
                "market_key": f"SYNTH-{index % 31:02d}",
                "observed_offset_us": index * 1_000,
                "active": active,
                "value_ppm": (index * 104_729 + len(name) * 7_919) % 1_000_001,
            }
        )
    return rows


def build_pack() -> dict[str, Any]:
    fixtures: list[dict[str, Any]] = []
    manifest_entries: list[dict[str, Any]] = []
    for name in FIXTURE_NAMES:
        specification = SPECIFICATIONS[name]
        fixture: dict[str, Any] = {
            "schema": FIXTURE_SCHEMA,
            "name": name,
            "seed": f"phase4bv-{name}-v1",
            "specification": specification,
            "expected_envelope": ENVELOPES[name],
            "events": _events(name, specification),
            "production_records_created": 0,
            "execution_authorized": False,
        }
        fixture["artifact_hash"] = _hash(fixture)
        fixtures.append(fixture)
        manifest_entries.append(
            {
                "name": name,
                "filename": f"phase4bv-{name}.json",
                "event_count": specification["event_count"],
                "fixture_hash": fixture["artifact_hash"],
                "expected_envelope": ENVELOPES[name],
            }
        )
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "phase": "4BV",
        "fixture_count": len(fixtures),
        "fixtures": manifest_entries,
        "synthetic_only": True,
        "execution_authorized": False,
    }
    manifest["artifact_hash"] = _hash(manifest)
    pack: dict[str, Any] = {
        "schema": PACK_SCHEMA,
        "manifest": manifest,
        "fixtures": fixtures,
        "manifest_hash": manifest["artifact_hash"],
    }
    pack["artifact_hash"] = _hash(pack)
    validate_pack(pack)
    return pack


def validate_pack(pack: dict[str, Any]) -> None:
    if pack.get("schema") != PACK_SCHEMA or pack.get("artifact_hash") != _hash(pack):
        raise ValueError("PHASE4BV_PACK_SCHEMA_OR_HASH_INVALID")
    manifest = pack.get("manifest")
    fixtures = pack.get("fixtures")
    if not isinstance(manifest, dict) or not isinstance(fixtures, list):
        raise ValueError("PHASE4BV_PACK_SHAPE_INVALID")
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("artifact_hash") != _hash(manifest)
    ):
        raise ValueError("PHASE4BV_MANIFEST_SCHEMA_OR_HASH_INVALID")
    if pack.get("manifest_hash") != manifest["artifact_hash"]:
        raise ValueError("PHASE4BV_MANIFEST_LINK_INVALID")
    if manifest.get("fixture_count") != len(FIXTURE_NAMES) or len(fixtures) != len(FIXTURE_NAMES):
        raise ValueError("PHASE4BV_FIXTURE_COUNT_INVALID")
    entries = manifest.get("fixtures")
    if not isinstance(entries, list) or [row.get("name") for row in entries] != list(FIXTURE_NAMES):
        raise ValueError("PHASE4BV_MANIFEST_ORDER_INVALID")
    if [row.get("name") for row in fixtures if isinstance(row, dict)] != list(FIXTURE_NAMES):
        raise ValueError("PHASE4BV_FIXTURE_ORDER_INVALID")
    for fixture, entry in zip(fixtures, entries, strict=True):
        name = fixture.get("name")
        if (
            fixture.get("schema") != FIXTURE_SCHEMA
            or fixture.get("artifact_hash") != _hash(fixture)
        ):
            raise ValueError("PHASE4BV_FIXTURE_SCHEMA_OR_HASH_INVALID")
        if name not in SPECIFICATIONS or fixture.get("specification") != SPECIFICATIONS[name]:
            raise ValueError("PHASE4BV_SPECIFICATION_INVALID")
        if fixture.get("expected_envelope") != ENVELOPES[name]:
            raise ValueError("PHASE4BV_ENVELOPE_INVALID")
        events = fixture.get("events")
        expected_count = SPECIFICATIONS[name]["event_count"]
        if not isinstance(events, list) or len(events) != expected_count:
            raise ValueError("PHASE4BV_EVENT_COUNT_INVALID")
        if [row.get("sequence") for row in events if isinstance(row, dict)] != list(
            range(expected_count)
        ):
            raise ValueError("PHASE4BV_EVENT_SEQUENCE_INVALID")
        if entry != {
            "name": name,
            "filename": f"phase4bv-{name}.json",
            "event_count": expected_count,
            "fixture_hash": fixture["artifact_hash"],
            "expected_envelope": ENVELOPES[name],
        }:
            raise ValueError("PHASE4BV_MANIFEST_ENTRY_INVALID")
        if (
            fixture.get("production_records_created") != 0
            or fixture.get("execution_authorized") is not False
        ):
            raise ValueError("PHASE4BV_SAFETY_INVARIANT_INVALID")


def publish_pack(directory: Path, pack: dict[str, Any]) -> None:
    validate_pack(pack)
    directory.mkdir(parents=True, exist_ok=True)
    documents = {
        **{
            entry["filename"]: fixture
            for entry, fixture in zip(pack["manifest"]["fixtures"], pack["fixtures"], strict=True)
        },
        "phase4bv-manifest.json": pack["manifest"],
        "phase4bv-pack.json": pack,
    }
    staged: list[tuple[Path, Path]] = []
    try:
        for filename, payload in documents.items():
            descriptor, temporary = tempfile.mkstemp(prefix=f".{filename}.", dir=directory)
            temporary_path = Path(temporary)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((temporary_path, directory / filename))
        for temporary_path, destination in staged:
            os.replace(temporary_path, destination)
    finally:
        for temporary_path, _ in staged:
            temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    pack = build_pack()
    publish_pack(args.output_directory, pack)
    print(json.dumps(pack["manifest"], sort_keys=True))


if __name__ == "__main__":
    main()
