"""Phase 4AC append-only artifact history with anchored bounded retention."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ENTRY_SCHEMA = "phase4ac.artifact-history-entry.v1"
MANIFEST_SCHEMA = "phase4ac.artifact-history-manifest.v1"
SOURCE_SCHEMAS = {
    "PHASE4AA_GATE": "phase4aa.settlement-closure-gate.v1",
    "PHASE4AB_DWELL": "phase4ab.settlement-closure-dwell.v1",
}
ZERO_HASH = "0" * 64


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _artifact_hash(payload: dict[str, Any]) -> str:
    return _hash({key: value for key, value in payload.items() if key != "artifact_hash"})


def _utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _validate_source(payload: dict[str, Any], kind: str) -> None:
    if kind not in SOURCE_SCHEMAS or payload.get("schema") != SOURCE_SCHEMAS[kind]:
        raise ValueError("PHASE4AC_SOURCE_SCHEMA_INVALID")
    if payload.get("artifact_hash") != _artifact_hash(payload):
        raise ValueError("PHASE4AC_SOURCE_HASH_MISMATCH")
    if payload.get("production_database_written") is not False:
        raise ValueError("PHASE4AC_SOURCE_PRODUCTION_WRITE_FLAG_INVALID")
    if payload.get("trading_mode_changed") is not False:
        raise ValueError("PHASE4AC_SOURCE_TRADING_FLAG_INVALID")
    _utc(payload["generated_at"])


def _manifest_hash(payload: dict[str, Any]) -> str:
    return _hash({key: value for key, value in payload.items() if key != "manifest_hash"})


def _entry_hash(payload: dict[str, Any]) -> str:
    return _hash({key: value for key, value in payload.items() if key != "entry_hash"})


def _filename(entry: dict[str, Any]) -> str:
    timestamp = _utc(entry["generated_at"]).strftime("%Y%m%dT%H%M%S%fZ")
    kind = str(entry["kind"]).lower().replace("_", "-")
    return (
        f"{int(entry['sequence']):020d}-{timestamp}-{kind}-"
        f"{str(entry['source_artifact_hash'])[:16]}.json"
    )


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _empty_manifest() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "total_entries": 0,
        "retained_count": 0,
        "first_sequence": None,
        "last_sequence": None,
        "anchor_previous_entry_hash": ZERO_HASH,
        "head_entry_hash": ZERO_HASH,
        "seen_source_artifact_hashes": [],
        "entries": [],
    }
    payload["manifest_hash"] = _manifest_hash(payload)
    return payload


def _load_manifest(history_dir: Path) -> dict[str, Any]:
    path = history_dir / "manifest.json"
    if not path.exists():
        return _empty_manifest()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("PHASE4AC_MANIFEST_SCHEMA_INVALID")
    if payload.get("manifest_hash") != _manifest_hash(payload):
        raise ValueError("PHASE4AC_MANIFEST_HASH_MISMATCH")
    return payload


def validate_history(history_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _load_manifest(history_dir)
    references = manifest.get("entries")
    if not isinstance(references, list):
        raise ValueError("PHASE4AC_MANIFEST_ENTRIES_INVALID")
    if int(manifest["retained_count"]) != len(references):
        raise ValueError("PHASE4AC_MANIFEST_RETAINED_COUNT_MISMATCH")
    seen_history = manifest.get("seen_source_artifact_hashes")
    if not isinstance(seen_history, list) or len(seen_history) != len(set(seen_history)):
        raise ValueError("PHASE4AC_MANIFEST_SEEN_SOURCES_INVALID")
    if len(seen_history) != int(manifest["total_entries"]):
        raise ValueError("PHASE4AC_MANIFEST_SEEN_SOURCE_COUNT_MISMATCH")
    entries: list[dict[str, Any]] = []
    previous_hash = str(manifest["anchor_previous_entry_hash"])
    previous_sequence: int | None = None
    previous_at: datetime | None = None
    seen_sources: set[str] = set()
    for reference in references:
        filename = str(reference["filename"])
        path = history_dir / filename
        if not path.is_file():
            raise ValueError("PHASE4AC_ENTRY_MISSING")
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("schema") != ENTRY_SCHEMA:
            raise ValueError("PHASE4AC_ENTRY_SCHEMA_INVALID")
        if filename != _filename(entry):
            raise ValueError("PHASE4AC_ENTRY_FILENAME_INVALID")
        if entry.get("entry_hash") != _entry_hash(entry):
            raise ValueError("PHASE4AC_ENTRY_HASH_MISMATCH")
        if reference.get("entry_hash") != entry["entry_hash"]:
            raise ValueError("PHASE4AC_MANIFEST_ENTRY_HASH_MISMATCH")
        if entry.get("previous_entry_hash") != previous_hash:
            raise ValueError("PHASE4AC_ENTRY_LINK_MISSING")
        sequence = int(entry["sequence"])
        generated_at = _utc(entry["generated_at"])
        if previous_sequence is not None and sequence != previous_sequence + 1:
            raise ValueError("PHASE4AC_ENTRY_SEQUENCE_GAP")
        if previous_at is not None and generated_at <= previous_at:
            raise ValueError("PHASE4AC_ENTRY_TIMESTAMP_OUT_OF_ORDER")
        source = entry.get("source_payload")
        if not isinstance(source, dict):
            raise ValueError("PHASE4AC_ENTRY_SOURCE_MISSING")
        _validate_source(source, str(entry["kind"]))
        if source["artifact_hash"] != entry["source_artifact_hash"]:
            raise ValueError("PHASE4AC_ENTRY_SOURCE_HASH_MISMATCH")
        if source["artifact_hash"] in seen_sources:
            raise ValueError("PHASE4AC_ENTRY_DUPLICATE_SOURCE")
        seen_sources.add(source["artifact_hash"])
        if source["artifact_hash"] not in seen_history:
            raise ValueError("PHASE4AC_ENTRY_SOURCE_NOT_INDEXED")
        entries.append(entry)
        previous_hash = entry["entry_hash"]
        previous_sequence = sequence
        previous_at = generated_at
    if entries:
        if manifest["head_entry_hash"] != entries[-1]["entry_hash"]:
            raise ValueError("PHASE4AC_MANIFEST_HEAD_MISMATCH")
        if manifest["first_sequence"] != entries[0]["sequence"]:
            raise ValueError("PHASE4AC_MANIFEST_FIRST_SEQUENCE_MISMATCH")
        if manifest["last_sequence"] != entries[-1]["sequence"]:
            raise ValueError("PHASE4AC_MANIFEST_LAST_SEQUENCE_MISMATCH")
    elif manifest["head_entry_hash"] != ZERO_HASH:
        raise ValueError("PHASE4AC_EMPTY_MANIFEST_HEAD_INVALID")
    return manifest, entries


def append(
    history_dir: Path,
    source_path: Path,
    *,
    kind: str,
    retention: int,
) -> dict[str, Any]:
    if retention < 1:
        raise ValueError("PHASE4AC_RETENTION_INVALID")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    _validate_source(source, kind)
    manifest, entries = validate_history(history_dir)
    seen_history = list(manifest["seen_source_artifact_hashes"])
    if source["artifact_hash"] in seen_history:
        raise ValueError("PHASE4AC_DUPLICATE_SNAPSHOT")
    generated_at = _utc(source["generated_at"])
    if entries and generated_at <= _utc(entries[-1]["generated_at"]):
        raise ValueError("PHASE4AC_SOURCE_TIMESTAMP_OUT_OF_ORDER")
    sequence = int(manifest["total_entries"]) + 1
    previous_hash = entries[-1]["entry_hash"] if entries else manifest["head_entry_hash"]
    entry: dict[str, Any] = {
        "schema": ENTRY_SCHEMA,
        "sequence": sequence,
        "kind": kind,
        "generated_at": generated_at.isoformat(),
        "source_artifact_hash": source["artifact_hash"],
        "previous_entry_hash": previous_hash,
        "source_payload": source,
    }
    entry["entry_hash"] = _entry_hash(entry)
    filename = _filename(entry)
    entry_path = history_dir / filename
    if entry_path.exists():
        raise ValueError("PHASE4AC_ENTRY_FILENAME_COLLISION")
    _write_atomic(entry_path, entry)
    combined = entries + [entry]
    retained = combined[-retention:]
    retained_filenames = {_filename(item) for item in retained}
    anchor = retained[0]["previous_entry_hash"]
    references = [
        {
            "filename": _filename(item),
            "entry_hash": item["entry_hash"],
            "sequence": item["sequence"],
            "kind": item["kind"],
            "generated_at": item["generated_at"],
            "source_artifact_hash": item["source_artifact_hash"],
        }
        for item in retained
    ]
    new_manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "total_entries": sequence,
        "retained_count": len(retained),
        "first_sequence": retained[0]["sequence"],
        "last_sequence": retained[-1]["sequence"],
        "anchor_previous_entry_hash": anchor,
        "head_entry_hash": retained[-1]["entry_hash"],
        "seen_source_artifact_hashes": seen_history + [source["artifact_hash"]],
        "entries": references,
    }
    new_manifest["manifest_hash"] = _manifest_hash(new_manifest)
    _write_atomic(history_dir / "manifest.json", new_manifest)
    for old_entry in combined:
        old_path = history_dir / _filename(old_entry)
        if old_path.name not in retained_filenames and old_path.is_file():
            old_path.unlink()
    return new_manifest


def reconstruct_phase4ab_history(history_dir: Path) -> dict[str, Any]:
    manifest, entries = validate_history(history_dir)
    gates = [entry["source_payload"] for entry in entries if entry["kind"] == "PHASE4AA_GATE"]
    return {
        "schema": "phase4ac.phase4ab-history-input.v1",
        "manifest_hash": manifest["manifest_hash"],
        "anchor_previous_entry_hash": manifest["anchor_previous_entry_hash"],
        "retained_entries": len(entries),
        "phase4aa_gate_count": len(gates),
        "phase4aa_gate_artifact_hashes": [gate["artifact_hash"] for gate in gates],
        "phase4aa_gate_payloads": gates,
        "validated": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    append_parser = subparsers.add_parser("append")
    append_parser.add_argument("--history-dir", type=Path, required=True)
    append_parser.add_argument("--source-artifact", type=Path, required=True)
    append_parser.add_argument("--kind", choices=sorted(SOURCE_SCHEMAS), required=True)
    append_parser.add_argument("--retention", type=int, default=96)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--history-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "append":
        result = append(
            args.history_dir,
            args.source_artifact,
            kind=args.kind,
            retention=args.retention,
        )
    else:
        result = reconstruct_phase4ab_history(args.history_dir)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
