from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.ui.prov14b_pipeline_status import normalize_prov14b_pipeline

DEFAULT_HISTORY_LIMIT = 96
STALE_GAP_SECONDS = 45
MAX_HISTORY_BYTES = 4_194_304


def history_path_for(snapshot_path: Path) -> Path:
    return snapshot_path.with_suffix(snapshot_path.suffix + ".history.json")


def _digest(snapshot: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed


def _compact(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    process = snapshot.get("active_process") or {}
    writer = snapshot.get("writer") or {}
    scheduler = snapshot.get("scheduler") or {}
    backup = snapshot.get("backup") or {}
    prov14b = _compact_prov14b(snapshot)
    return {
        "generated_at": snapshot.get("generated_at"),
        "snapshot_digest": _digest(snapshot),
        "process": {
            "state": process.get("state"),
            "name": process.get("name"),
            "pid": process.get("pid"),
            "stage": process.get("stage"),
            "completion_evidence": process.get("completion_evidence"),
        },
        "writer": {
            "state": writer.get("state"),
            "pid": writer.get("pid"),
            "lock_status": writer.get("lock_status"),
            "safe_to_start_write": writer.get("safe_to_start_write"),
        },
        "scheduler": {
            "state": scheduler.get("state"),
            "cycle": scheduler.get("cycle"),
            "stage": scheduler.get("stage"),
        },
        "backup": {"state": backup.get("state"), "integrity": backup.get("integrity")},
        "execution_enabled": bool(snapshot.get("execution_enabled")),
        "alert_codes": sorted(
            {
                str(item.get("code"))
                for item in snapshot.get("alerts", [])
                if isinstance(item, dict) and item.get("code")
            }
        ),
        "prov14b": prov14b,
    }


def _compact_prov14b(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    try:
        reference_time = _parse(str(snapshot.get("generated_at")))
        normalized, _ = normalize_prov14b_pipeline(dict(snapshot), reference_time=reference_time)
    except (TypeError, ValueError):
        return {"state": "BLOCKED", "current_stage": "invalid timestamp", "gates": {}, "alerts": []}
    return {
        "state": normalized["state"],
        "current_stage": normalized["current_stage"],
        "gates": {
            gate["id"]: {
                "state": gate["state"],
                "evidence_stale": gate["evidence_stale"],
                "evidence_age_seconds": gate["evidence_age_seconds"],
            }
            for gate in normalized["gates"]
        },
        "alerts": [alert["code"] for alert in normalized["alerts"]],
    }


def _incidents(
    previous: Mapping[str, Any] | None, current: Mapping[str, Any]
) -> list[dict[str, Any]]:
    incidents = []
    if previous:
        gap = int(
            (_parse(current["generated_at"]) - _parse(previous["generated_at"])).total_seconds()
        )
        if gap > STALE_GAP_SECONDS:
            incidents.append(
                {
                    "severity": "WARNING",
                    "code": "COLLECTION_GAP",
                    "message": f"Status collection gap was {gap}s.",
                }
            )
        for component in ("process", "writer", "scheduler", "backup"):
            before = (previous.get(component) or {}).get("state")
            after = (current.get(component) or {}).get("state")
            if before != after:
                severity = (
                    "CRITICAL" if after == "FAILED" else "WARNING" if after == "BLOCKED" else "INFO"
                )
                incidents.append(
                    {
                        "severity": severity,
                        "code": f"{component.upper()}_STATE_CHANGED",
                        "message": f"{component} changed from {before} to {after}.",
                    }
                )
        if (
            (previous.get("process") or {}).get("state") == "RUNNING"
            and (current.get("process") or {}).get("state") != "RUNNING"
            and not (current.get("process") or {}).get("completion_evidence")
        ):
            incidents.append(
                {
                    "severity": "CRITICAL",
                    "code": "PROCESS_DISAPPEARED_WITHOUT_EVIDENCE",
                    "message": "Running process disappeared without certified completion evidence.",
                }
            )
    if current.get("execution_enabled"):
        incidents.append(
            {
                "severity": "CRITICAL",
                "code": "EXECUTION_ENABLED",
                "message": "Execution became enabled.",
            }
        )
    previous_alerts = set(previous.get("alert_codes", [])) if previous else set()
    for code in sorted(set(current.get("alert_codes", [])) - previous_alerts):
        incidents.append(
            {"severity": "WARNING", "code": "ALERT_OPENED", "message": f"Alert opened: {code}."}
        )
    return incidents


def record_progress_snapshot(
    snapshot: Mapping[str, Any],
    history_path: Path,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> dict[str, Any]:
    if not 3 <= limit <= 1000:
        raise ValueError("history limit must be between 3 and 1000")
    temporary = history_path.with_suffix(history_path.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        if history_path.stat().st_size > MAX_HISTORY_BYTES:
            raise ValueError("HISTORY_TOO_LARGE")
        history = json.loads(history_path.read_text(encoding="utf-8"))
        entries = list(history.get("entries") or [])
    except FileNotFoundError:
        entries = []
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        entries = []
    current = _compact(snapshot)
    if entries and entries[-1].get("snapshot_digest") == current["snapshot_digest"]:
        return {"appended": False, "entries": entries, "retention_limit": limit}
    current["incidents"] = _incidents(entries[-1] if entries else None, current)
    entries.append(current)
    entries = entries[-limit:]
    payload = {"schema_version": 1, "retention_limit": limit, "entries": entries}
    history_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, history_path)
    return {
        "appended": True,
        "entries": entries,
        "retention_limit": limit,
        "history_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
    }


def load_progress_timeline(history_path: Path, *, limit: int = 20) -> dict[str, Any]:
    try:
        if history_path.stat().st_size > MAX_HISTORY_BYTES:
            raise ValueError("HISTORY_TOO_LARGE")
        payload = json.loads(history_path.read_text(encoding="utf-8"))
        entries = list(payload.get("entries") or [])[-limit:]
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError, AttributeError):
        entries = []
    timeline = []
    for entry in reversed(entries):
        incidents = entry.get("incidents") or []
        timeline.append(
            {
                "generated_at": entry.get("generated_at"),
                "process_state": (entry.get("process") or {}).get("state"),
                "process_name": (entry.get("process") or {}).get("name"),
                "writer_state": (entry.get("writer") or {}).get("state"),
                "scheduler_cycle": (entry.get("scheduler") or {}).get("cycle"),
                "incidents": incidents,
                "highest_severity": "CRITICAL"
                if any(i.get("severity") == "CRITICAL" for i in incidents)
                else "WARNING"
                if incidents
                else "INFO",
            }
        )
    return {"entries": timeline, "count": len(entries), "history_path": str(history_path)}
