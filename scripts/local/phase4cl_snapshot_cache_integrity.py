"""Verify an offline snapshot-cache artifact without reading or populating a cache."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cl.cache-manifest.v1"
REPORT_SCHEMA = "phase4cl.cache-integrity-report.v1"
MAX_ENTRIES = 10_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _hex_digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def expected_cache_key(namespace: str, identity: dict[str, Any]) -> str:
    return canonical_hash({"namespace": namespace, "identity": identity})


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "entries", "artifact_hash"}:
        raise ValueError("PHASE4CL_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CL_INPUT_SCHEMA_OR_HASH_INVALID")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
        raise ValueError("PHASE4CL_ENTRY_COUNT_INVALID")

    seen_keys: set[str] = set()
    seen_identities: dict[str, str] = {}
    decisions = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "namespace",
            "identity",
            "cache_key",
            "value",
            "value_hash",
        }:
            raise ValueError("PHASE4CL_ENTRY_FIELDS_INVALID")
        namespace = entry["namespace"]
        identity = entry["identity"]
        key = entry["cache_key"]
        value_hash = entry["value_hash"]
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("PHASE4CL_NAMESPACE_INVALID")
        if not isinstance(identity, dict) or not identity:
            raise ValueError("PHASE4CL_IDENTITY_INVALID")
        if not _hex_digest(key) or not _hex_digest(value_hash):
            raise ValueError("PHASE4CL_DIGEST_FORMAT_INVALID")

        reasons = []
        calculated_key = expected_cache_key(namespace, identity)
        calculated_value_hash = canonical_hash(entry["value"])
        identity_hash = canonical_hash({"namespace": namespace, "identity": identity})
        if key != calculated_key:
            reasons.append("CACHE_KEY_MISMATCH")
        if value_hash != calculated_value_hash:
            reasons.append("VALUE_HASH_MISMATCH")
        if key in seen_keys:
            reasons.append("DUPLICATE_CACHE_KEY")
        previous_key = seen_identities.get(identity_hash)
        if previous_key is not None and previous_key != key:
            reasons.append("IDENTITY_KEY_DIVERGENCE")
        seen_keys.add(key)
        seen_identities[identity_hash] = key
        decisions.append(
            {
                "cache_key": key,
                "calculated_cache_key": calculated_key,
                "calculated_value_hash": calculated_value_hash,
                "status": "INVALID" if reasons else "VALID",
                "reasons": sorted(reasons),
            }
        )

    invalid = sum(row["status"] == "INVALID" for row in decisions)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CL",
        "input_hash": payload["artifact_hash"],
        "status": "FAIL_CLOSED" if invalid else "PASS",
        "cache_usable": invalid == 0,
        "entry_count": len(entries),
        "invalid_count": invalid,
        "entries": decisions,
        "cache_reads_performed": 0,
        "cache_writes_performed": 0,
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
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.manifest.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
