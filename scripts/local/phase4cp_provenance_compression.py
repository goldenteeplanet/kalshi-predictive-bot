"""Compress repeated provenance using reconstructable content-addressed references."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cp.provenance-input.v1"
REPORT_SCHEMA = "phase4cp.provenance-compression.v1"
MAX_RECORDS = 20_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _bytes(value: Any) -> int:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return len(encoded)


def reconstruct(report: dict[str, Any]) -> list[dict[str, Any]]:
    if report.get("schema") != REPORT_SCHEMA or report.get("artifact_hash") != _hash(report):
        raise ValueError("PHASE4CP_REPORT_SCHEMA_OR_HASH_INVALID")
    dictionary = report.get("provenance_dictionary")
    records = report.get("compressed_records")
    if not isinstance(dictionary, dict) or not isinstance(records, list):
        raise ValueError("PHASE4CP_REPORT_SHAPE_INVALID")
    for digest, provenance in dictionary.items():
        if digest != canonical_hash(provenance):
            raise ValueError("PHASE4CP_DICTIONARY_HASH_INVALID")
    restored = []
    identifiers: set[str] = set()
    for record in records:
        required = {"record_id", "payload", "provenance_ref"}
        if not isinstance(record, dict) or set(record) != required:
            raise ValueError("PHASE4CP_COMPRESSED_RECORD_INVALID")
        identifier = record["record_id"]
        reference = record["provenance_ref"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4CP_RECORD_ID_INVALID")
        if reference not in dictionary:
            raise ValueError("PHASE4CP_PROVENANCE_REFERENCE_MISSING")
        identifiers.add(identifier)
        restored.append(
            {
                "record_id": identifier,
                "payload": record["payload"],
                "provenance": dictionary[reference],
            }
        )
    return restored


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "records", "artifact_hash"}:
        raise ValueError("PHASE4CP_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CP_INPUT_SCHEMA_OR_HASH_INVALID")
    records = payload.get("records")
    if not isinstance(records, list) or not records or len(records) > MAX_RECORDS:
        raise ValueError("PHASE4CP_RECORD_COUNT_INVALID")
    identifiers: set[str] = set()
    dictionary: dict[str, Any] = {}
    compressed = []
    normalized = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"record_id", "payload", "provenance"}:
            raise ValueError("PHASE4CP_RECORD_FIELDS_INVALID")
        identifier = record["record_id"]
        provenance = record["provenance"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4CP_RECORD_ID_INVALID")
        if not isinstance(provenance, dict) or not provenance:
            raise ValueError("PHASE4CP_PROVENANCE_INVALID")
        identifiers.add(identifier)
        reference = canonical_hash(provenance)
        existing = dictionary.get(reference)
        if existing is not None and existing != provenance:
            raise ValueError("PHASE4CP_CONTENT_HASH_COLLISION")
        dictionary[reference] = provenance
        normalized.append(record)
        compressed.append(
            {"record_id": identifier, "payload": record["payload"], "provenance_ref": reference}
        )
    ordered_dictionary = {key: dictionary[key] for key in sorted(dictionary)}
    baseline_bytes = _bytes(normalized)
    compressed_bytes = _bytes(
        {"provenance_dictionary": ordered_dictionary, "compressed_records": compressed}
    )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CP",
        "input_hash": payload["artifact_hash"],
        "provenance_dictionary": ordered_dictionary,
        "compressed_records": compressed,
        "record_count": len(normalized),
        "unique_provenance_count": len(dictionary),
        "baseline_bytes": baseline_bytes,
        "compressed_bytes": compressed_bytes,
        "bytes_saved": baseline_bytes - compressed_bytes,
        "reconstruction_hash": canonical_hash(normalized),
        "reconstructable": True,
        "execution_authorized": False,
        "production_records_created": 0,
    }
    report["artifact_hash"] = _hash(report)
    restored = reconstruct(report)
    if restored != normalized or canonical_hash(restored) != report["reconstruction_hash"]:
        raise ValueError("PHASE4CP_RECONSTRUCTION_FAILED")
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
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.records.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
