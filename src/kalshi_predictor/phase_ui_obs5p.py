"""Offline certification exports for the PROV-14B transition timeline."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.ui.prov14b_certification_history import (
    load_prov14b_certification_timeline,
)

SCHEMA_VERSION = 1
CSV_FIELDS = ("timestamp", "subject", "event_type", "before", "after", "resolved")


def certify_timeline_export(history_path: Path, output_dir: Path) -> dict[str, Any]:
    """Export and certify one immutable local history capture."""
    source_bytes = history_path.read_bytes()
    source_sha = _sha256(source_bytes)
    source_payload = json.loads(source_bytes)
    timeline = load_prov14b_certification_timeline(history_path)
    failures = _retention_failures(source_payload) + _transition_failures(timeline)
    if _sha256(history_path.read_bytes()) != source_sha:
        failures.append("SOURCE_DRIFT_DURING_EXPORT")

    output_dir.mkdir(parents=True, exist_ok=True)
    json_bytes = _json_bytes(timeline)
    csv_bytes = _csv_bytes(timeline["events"])
    json_sha = _sha256(json_bytes)
    csv_sha = _sha256(csv_bytes)
    bundle_sha = _sha256(f"{source_sha}\n{json_sha}\n{csv_sha}\n".encode())
    generated_at = _latest_timestamp(source_payload)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "phase": "UI-OBS-5P",
        "status": "PASSED" if not failures else "FAILED",
        "generated_at": generated_at,
        "source": {
            "name": history_path.name,
            "sha256": source_sha,
            "retention_limit": source_payload.get("retention_limit"),
            "entry_count": len(source_payload.get("entries") or []),
        },
        "exports": {
            "json": {"name": "certification_timeline.json", "sha256": json_sha},
            "csv": {"name": "certification_timeline.csv", "sha256": csv_sha},
        },
        "bundle_sha256": bundle_sha,
        "transition_count": timeline["event_count"],
        "failures": failures,
        "guardrails": {
            "database_access": False,
            "cloud_access": False,
            "runtime_controls": False,
            "execution_changed": False,
        },
    }
    _atomic_write(output_dir / "certification_timeline.json", json_bytes)
    _atomic_write(output_dir / "certification_timeline.csv", csv_bytes)
    _atomic_write(output_dir / "ui_obs5p_certification_manifest.json", _json_bytes(manifest))
    return manifest


def verify_timeline_bundle(output_dir: Path) -> dict[str, Any]:
    """Verify retained exports against their manifest without regenerating them."""
    manifest_path = output_dir / "ui_obs5p_certification_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"status": "FAILED", "failures": ["MANIFEST_MISSING_OR_INVALID"]}
    failures: list[str] = []
    hashes = []
    for kind in ("json", "csv"):
        metadata = (manifest.get("exports") or {}).get(kind) or {}
        path = output_dir / str(metadata.get("name") or "")
        try:
            actual = _sha256(path.read_bytes())
        except OSError:
            failures.append(f"{kind.upper()}_EXPORT_MISSING")
            continue
        hashes.append(actual)
        if actual != metadata.get("sha256"):
            failures.append(f"{kind.upper()}_EXPORT_HASH_MISMATCH")
    source_sha = str((manifest.get("source") or {}).get("sha256") or "")
    if len(hashes) == 2:
        bundle = _sha256(f"{source_sha}\n{hashes[0]}\n{hashes[1]}\n".encode())
        if bundle != manifest.get("bundle_sha256"):
            failures.append("BUNDLE_HASH_MISMATCH")
    if manifest.get("status") != "PASSED":
        failures.append("MANIFEST_NOT_CERTIFIED")
    return {
        "status": "PASSED" if not failures else "FAILED",
        "failures": failures,
        "bundle_sha256": manifest.get("bundle_sha256"),
    }


def _retention_failures(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["HISTORY_ROOT_INVALID"]
    failures: list[str] = []
    entries = payload.get("entries")
    limit = payload.get("retention_limit")
    if payload.get("schema_version") != 1:
        failures.append("HISTORY_SCHEMA_INVALID")
    if not isinstance(entries, list):
        return failures + ["HISTORY_ENTRIES_INVALID"]
    if not isinstance(limit, int) or not 3 <= limit <= 1000:
        failures.append("RETENTION_LIMIT_INVALID")
    elif len(entries) > limit:
        failures.append("RETENTION_LIMIT_EXCEEDED")
    digests = [entry.get("snapshot_digest") for entry in entries if isinstance(entry, dict)]
    if any(not isinstance(value, str) or len(value) != 64 for value in digests):
        failures.append("SNAPSHOT_DIGEST_INVALID")
    if len(digests) != len(set(digests)):
        failures.append("SNAPSHOT_DIGEST_DUPLICATE")
    timestamps = [_parse_timestamp(entry.get("generated_at")) for entry in entries]
    if any(value is None for value in timestamps):
        failures.append("HISTORY_TIMESTAMP_INVALID")
    elif timestamps != sorted(timestamps):
        failures.append("HISTORY_ORDER_INVALID")
    return failures


def _transition_failures(timeline: dict[str, Any]) -> list[str]:
    failures = [str(value) for value in timeline.get("diagnostics") or []]
    events = timeline.get("events")
    if not isinstance(events, list):
        return failures + ["TIMELINE_EVENTS_INVALID"]
    if timeline.get("event_count") != len(events):
        failures.append("TRANSITION_COUNT_MISMATCH")
    timestamps = [_parse_timestamp(event.get("timestamp")) for event in events]
    if any(value is None for value in timestamps):
        failures.append("TRANSITION_TIMESTAMP_INVALID")
    elif timestamps != sorted(timestamps, reverse=True):
        failures.append("TRANSITION_ORDER_INVALID")
    for event in events:
        if bool(event.get("resolved")) != (event.get("event_type") == "ALERT_RESOLVED"):
            failures.append("RESOLUTION_FLAG_INCONSISTENT")
            break
    duration = timeline.get("duration_seconds")
    if duration is not None and (not isinstance(duration, int) or duration < 0):
        failures.append("CERTIFICATION_DURATION_INVALID")
    return sorted(set(failures))


def _csv_bytes(events: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for event in events:
        writer.writerow({field: event.get(field) for field in CSV_FIELDS})
    return stream.getvalue().encode("utf-8")


def _latest_timestamp(payload: dict[str, Any]) -> str | None:
    entries = payload.get("entries") or []
    return str(entries[-1].get("generated_at")) if entries else None


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, value: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
