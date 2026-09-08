"""Phase 4AZ generic append-only checkpoint history with safe bounded retention."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

ENTRY_SCHEMA = "phase4az.history-entry.v1"
MANIFEST_SCHEMA = "phase4az.history-manifest.v1"
CHECKPOINT_SCHEMA = "phase4az.checkpoint.v1"
PRUNING_SCHEMA = "phase4az.pruning-proof.v1"
BUNDLE_SCHEMA = "phase4az.tamper-evident-archive-bundle.v1"
MARKER_SCHEMA = "phase4az.disposable-history-directory.v1"
ZERO_HASH = "0" * 64


def _hash(payload: Any, field: str | None = None) -> str:
    if field is not None and isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != field}
    return canonical_hash(payload)


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("PHASE4AZ_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4AZ_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("PHASE4AZ_TIMESTAMP_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def _atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _marker(directory: Path, *, create: bool = False) -> None:
    path = directory / ".phase4az-disposable-history.json"
    if create and not path.exists():
        payload = {"schema": MARKER_SCHEMA, "disposable_history": True}
        payload["artifact_hash"] = _hash(payload, "artifact_hash")
        _atomic(path, payload)
    if not path.is_file():
        raise ValueError("PHASE4AZ_HISTORY_MARKER_MISSING")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != MARKER_SCHEMA
        or payload.get("disposable_history") is not True
        or payload.get("artifact_hash") != _hash(payload, "artifact_hash")
    ):
        raise ValueError("PHASE4AZ_HISTORY_MARKER_INVALID")


def _filename(sequence: int, timestamp: datetime, source_hash: str) -> str:
    stamp = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    return f"entry-{sequence:020d}-{stamp}-{source_hash[:16]}.json"


def _empty() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "total_entries": 0,
        "retained_count": 0,
        "first_sequence": None,
        "last_sequence": None,
        "anchor_previous_entry_hash": ZERO_HASH,
        "head_entry_hash": ZERO_HASH,
        "previous_checkpoint_hash": ZERO_HASH,
        "manifest_root": ZERO_HASH,
        "seen_source_hashes": [],
        "entries": [],
    }
    payload["manifest_hash"] = _hash(payload, "manifest_hash")
    return payload


def _manifest(directory: Path) -> dict[str, Any]:
    path = directory / "manifest.json"
    if not path.exists():
        return _empty()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != MANIFEST_SCHEMA or payload.get("manifest_hash") != _hash(
        payload, "manifest_hash"
    ):
        raise ValueError("PHASE4AZ_MANIFEST_INVALID")
    return payload


def validate(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _marker(directory)
    manifest = _manifest(directory)
    references = manifest.get("entries")
    seen = manifest.get("seen_source_hashes")
    if (
        not isinstance(references, list)
        or not isinstance(seen, list)
        or len(seen) != len(set(seen))
    ):
        raise ValueError("PHASE4AZ_MANIFEST_CONTENT_INVALID")
    if len(references) != manifest["retained_count"] or len(seen) != manifest["total_entries"]:
        raise ValueError("PHASE4AZ_MANIFEST_COUNT_INVALID")
    previous_checkpoint = ZERO_HASH
    for checkpoint_sequence in range(1, manifest["total_entries"] + 1):
        checkpoint_path = directory / f"checkpoint-{checkpoint_sequence:020d}.json"
        pruning_path = directory / f"pruning-{checkpoint_sequence:020d}.json"
        if not checkpoint_path.is_file() or not pruning_path.is_file():
            raise ValueError("PHASE4AZ_CHECKPOINT_OR_PRUNING_PROOF_MISSING")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        pruning = json.loads(pruning_path.read_text(encoding="utf-8"))
        if (
            checkpoint.get("schema") != CHECKPOINT_SCHEMA
            or checkpoint.get("checkpoint_hash") != _hash(checkpoint, "checkpoint_hash")
            or checkpoint.get("sequence") != checkpoint_sequence
            or checkpoint.get("previous_checkpoint_hash") != previous_checkpoint
        ):
            raise ValueError("PHASE4AZ_CHECKPOINT_INVALID")
        if (
            pruning.get("schema") != PRUNING_SCHEMA
            or pruning.get("artifact_hash") != _hash(pruning, "artifact_hash")
            or pruning.get("checkpoint_hash") != checkpoint["checkpoint_hash"]
        ):
            raise ValueError("PHASE4AZ_PRUNING_PROOF_INVALID")
        previous_checkpoint = checkpoint["checkpoint_hash"]
    if previous_checkpoint != manifest["previous_checkpoint_hash"]:
        raise ValueError("PHASE4AZ_CHECKPOINT_HEAD_INVALID")
    entries: list[dict[str, Any]] = []
    previous = manifest["anchor_previous_entry_hash"]
    previous_sequence: int | None = None
    previous_time: datetime | None = None
    retained_sources: set[str] = set()
    for reference in references:
        path = directory / str(reference.get("filename"))
        if not path.is_file():
            raise ValueError("PHASE4AZ_ENTRY_MISSING")
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("schema") != ENTRY_SCHEMA or entry.get("entry_hash") != _hash(
            entry, "entry_hash"
        ):
            raise ValueError("PHASE4AZ_ENTRY_HASH_OR_SCHEMA_INVALID")
        if path.name != _filename(
            entry["sequence"], _time(entry["timestamp"]), entry["source_hash"]
        ):
            raise ValueError("PHASE4AZ_ENTRY_FILENAME_INVALID")
        if (
            reference.get("entry_hash") != entry["entry_hash"]
            or entry["previous_entry_hash"] != previous
        ):
            raise ValueError("PHASE4AZ_ENTRY_LINK_INVALID")
        sequence, timestamp = int(entry["sequence"]), _time(entry["timestamp"])
        if previous_sequence is not None and sequence != previous_sequence + 1:
            raise ValueError("PHASE4AZ_SEQUENCE_GAP")
        if previous_time is not None and timestamp <= previous_time:
            raise ValueError("PHASE4AZ_TIMESTAMP_OUT_OF_ORDER")
        source = entry.get("source_payload")
        hash_field = entry.get("source_hash_field")
        if not isinstance(source, dict) or hash_field not in {"artifact_hash", "manifest_hash"}:
            raise ValueError("PHASE4AZ_SOURCE_INVALID")
        if (
            source.get(hash_field) != _hash(source, hash_field)
            or source[hash_field] != entry["source_hash"]
        ):
            raise ValueError("PHASE4AZ_SOURCE_HASH_INVALID")
        if entry["source_hash"] in retained_sources or entry["source_hash"] not in seen:
            raise ValueError("PHASE4AZ_DUPLICATE_OR_UNINDEXED_SOURCE")
        retained_sources.add(entry["source_hash"])
        entries.append(entry)
        previous, previous_sequence, previous_time = entry["entry_hash"], sequence, timestamp
    if entries:
        if manifest["head_entry_hash"] != entries[-1]["entry_hash"]:
            raise ValueError("PHASE4AZ_HEAD_INVALID")
        if (
            manifest["first_sequence"] != entries[0]["sequence"]
            or manifest["last_sequence"] != entries[-1]["sequence"]
        ):
            raise ValueError("PHASE4AZ_SEQUENCE_BOUNDS_INVALID")
        root = _hash([entry["entry_hash"] for entry in entries])
        if root != manifest["manifest_root"]:
            raise ValueError("PHASE4AZ_MANIFEST_ROOT_INVALID")
    return manifest, entries


def append(
    directory: Path, source_path: Path, *, phase: str, timestamp_field: str, retention: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if retention < 1:
        raise ValueError("PHASE4AZ_RETENTION_INVALID")
    if not phase.startswith("4A") or len(phase) != 3:
        raise ValueError("PHASE4AZ_PHASE_INVALID")
    directory.mkdir(parents=True, exist_ok=True)
    _marker(directory, create=True)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    hash_field = "artifact_hash" if "artifact_hash" in source else "manifest_hash"
    if source.get(hash_field) != _hash(source, hash_field):
        raise ValueError("PHASE4AZ_SOURCE_HASH_INVALID")
    timestamp = _time(source.get(timestamp_field))
    manifest, entries = validate(directory)
    if source[hash_field] in manifest["seen_source_hashes"]:
        raise ValueError("PHASE4AZ_DUPLICATE_SOURCE")
    if entries and timestamp <= _time(entries[-1]["timestamp"]):
        raise ValueError("PHASE4AZ_SOURCE_OUT_OF_ORDER")
    sequence = manifest["total_entries"] + 1
    previous = entries[-1]["entry_hash"] if entries else manifest["head_entry_hash"]
    entry: dict[str, Any] = {
        "schema": ENTRY_SCHEMA,
        "sequence": sequence,
        "phase": phase,
        "timestamp": timestamp.isoformat(),
        "source_hash": source[hash_field],
        "source_hash_field": hash_field,
        "previous_entry_hash": previous,
        "source_payload": source,
    }
    entry["entry_hash"] = _hash(entry, "entry_hash")
    filename = _filename(sequence, timestamp, entry["source_hash"])
    path = directory / filename
    if path.exists():
        raise FileExistsError("PHASE4AZ_ENTRY_COLLISION")
    _atomic(path, entry)
    retained = (entries + [entry])[-retention:]
    pruned = (entries + [entry])[:-retention] if len(entries) + 1 > retention else []
    references = [
        {
            "filename": _filename(item["sequence"], _time(item["timestamp"]), item["source_hash"]),
            "entry_hash": item["entry_hash"],
        }
        for item in retained
    ]
    root = _hash([item["entry_hash"] for item in retained])
    checkpoint: dict[str, Any] = {
        "schema": CHECKPOINT_SCHEMA,
        "sequence": sequence,
        "previous_checkpoint_hash": manifest["previous_checkpoint_hash"],
        "head_entry_hash": entry["entry_hash"],
        "manifest_root": root,
        "anchor_previous_entry_hash": retained[0]["previous_entry_hash"],
    }
    checkpoint["checkpoint_hash"] = _hash(checkpoint, "checkpoint_hash")
    new_manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "total_entries": sequence,
        "retained_count": len(retained),
        "first_sequence": retained[0]["sequence"],
        "last_sequence": retained[-1]["sequence"],
        "anchor_previous_entry_hash": retained[0]["previous_entry_hash"],
        "head_entry_hash": retained[-1]["entry_hash"],
        "previous_checkpoint_hash": checkpoint["checkpoint_hash"],
        "manifest_root": root,
        "seen_source_hashes": manifest["seen_source_hashes"] + [source[hash_field]],
        "entries": references,
    }
    new_manifest["manifest_hash"] = _hash(new_manifest, "manifest_hash")
    pruning: dict[str, Any] = {
        "schema": PRUNING_SCHEMA,
        "checkpoint_hash": checkpoint["checkpoint_hash"],
        "pruned_entry_hashes": [item["entry_hash"] for item in pruned],
        "retained_anchor_hash": retained[0]["previous_entry_hash"],
        "chain_integrity_preserved": True,
    }
    pruning["artifact_hash"] = _hash(pruning, "artifact_hash")
    _atomic(directory / f"checkpoint-{sequence:020d}.json", checkpoint)
    _atomic(directory / "manifest.json", new_manifest)
    _atomic(directory / f"pruning-{sequence:020d}.json", pruning)
    for item in pruned:
        old = directory / _filename(item["sequence"], _time(item["timestamp"]), item["source_hash"])
        if old.is_file():
            old.unlink()
    return new_manifest, checkpoint, pruning


def reconstruct(directory: Path) -> dict[str, Any]:
    manifest, entries = validate(directory)
    lineage = sorted(
        {
            value
            for entry in entries
            for key, value in entry["source_payload"].items()
            if key != entry["source_hash_field"]
            and key.endswith("_hash")
            and isinstance(value, str)
            and len(value) == 64
        }
    )
    bundle: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "manifest_hash": manifest["manifest_hash"],
        "checkpoint_hash": manifest["previous_checkpoint_hash"],
        "anchor_previous_entry_hash": manifest["anchor_previous_entry_hash"],
        "retained_entry_hashes": [entry["entry_hash"] for entry in entries],
        "cross_phase_lineage_hashes": lineage,
        "validated": True,
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    bundle["artifact_hash"] = _hash(bundle, "artifact_hash")
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("append")
    add.add_argument("--history-dir", type=Path, required=True)
    add.add_argument("--source-artifact", type=Path, required=True)
    add.add_argument("--phase", required=True)
    add.add_argument("--timestamp-field", default="evaluated_at")
    add.add_argument("--retention", type=int, default=128)
    verify = sub.add_parser("validate")
    verify.add_argument("--history-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "append":
        result = append(
            args.history_dir,
            args.source_artifact,
            phase=args.phase,
            timestamp_field=args.timestamp_field,
            retention=args.retention,
        )[0]
    else:
        result = reconstruct(args.history_dir)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
