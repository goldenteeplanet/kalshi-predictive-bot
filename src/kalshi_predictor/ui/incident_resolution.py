from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_HISTORY_BYTES = 4_194_304


def acknowledgment_path_for(history_path: Path) -> Path:
    return history_path.with_suffix(history_path.suffix + ".acknowledgments.json")


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(UTC)


def _incident_id(code: str, message: str) -> str:
    return hashlib.sha256(f"{code}|{message}".encode()).hexdigest()[:16]


def _escalated_severity(base: str, duration_seconds: int) -> tuple[str, str | None]:
    if base == "CRITICAL":
        return "CRITICAL", None
    if duration_seconds >= 3600:
        return "CRITICAL", "UNRESOLVED_60_MINUTES"
    if duration_seconds >= 900:
        return "HIGH", "UNRESOLVED_15_MINUTES"
    return base, None


def build_incident_resolution_preview(
    history_path: Path,
    acknowledgment_path: Path | None = None,
    *,
    as_of: str,
) -> dict[str, Any]:
    try:
        if history_path.stat().st_size > MAX_HISTORY_BYTES:
            raise ValueError("HISTORY_TOO_LARGE")
        history = json.loads(history_path.read_text(encoding="utf-8"))
        entries = list(history.get("entries") or [])
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError, AttributeError):
        entries = []
    ack_path = acknowledgment_path or acknowledgment_path_for(history_path)
    try:
        metadata = json.loads(ack_path.read_text(encoding="utf-8"))
        acknowledgments = list(metadata.get("acknowledgments") or [])
        resolutions = list(metadata.get("resolutions") or [])
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        acknowledgments, resolutions = [], []
    ack_by_id = {
        item.get("incident_id"): item for item in acknowledgments if item.get("incident_id")
    }
    resolution_by_id = {
        item.get("incident_id"): item for item in resolutions if item.get("incident_id")
    }
    now = _time(as_of)
    incidents: dict[str, dict[str, Any]] = {}
    for entry in entries:
        opened_at = entry.get("generated_at")
        for incident in entry.get("incidents") or []:
            code, message = str(incident.get("code")), str(incident.get("message"))
            incident_id = _incident_id(code, message)
            incidents.setdefault(
                incident_id,
                {
                    "incident_id": incident_id,
                    "code": code,
                    "message": message,
                    "base_severity": str(incident.get("severity") or "WARNING"),
                    "opened_at": opened_at,
                },
            )
    rows = []
    diagnostics = []
    for incident_id, incident in sorted(
        incidents.items(), key=lambda item: (item[1]["opened_at"], item[0])
    ):
        acknowledgment = ack_by_id.get(incident_id)
        resolution = resolution_by_id.get(incident_id)
        resolution_valid = bool(
            resolution
            and resolution.get("verified") is True
            and resolution.get("evidence_path")
            and isinstance(resolution.get("evidence_sha256"), str)
            and len(resolution["evidence_sha256"]) == 64
            and resolution.get("resolved_at")
        )
        if resolution and not resolution_valid:
            diagnostics.append(f"RESOLUTION_EVIDENCE_INVALID:{incident_id}")
        observed_only = incident["base_severity"] == "INFO"
        end = (
            _time(incident["opened_at"])
            if observed_only
            else _time(resolution["resolved_at"])
            if resolution_valid
            else now
        )
        duration = max(0, int((end - _time(incident["opened_at"])).total_seconds()))
        severity, escalation = (
            ("INFO", None)
            if observed_only
            else _escalated_severity(incident["base_severity"], duration)
        )
        critical_visible = severity == "CRITICAL"
        rows.append(
            {
                **incident,
                "status": "OBSERVED"
                if observed_only
                else "RESOLVED"
                if resolution_valid
                else "UNRESOLVED",
                "severity": severity,
                "escalation_reason": escalation,
                "duration_seconds": duration,
                "duration_minutes": duration // 60,
                "acknowledged": acknowledgment is not None,
                "acknowledgment": acknowledgment,
                "resolution": resolution if resolution_valid else None,
                "visible": True,
                "suppression_allowed": not critical_visible,
                "critical_visible_despite_acknowledgment": critical_visible
                and acknowledgment is not None,
            }
        )
    unresolved = [row for row in rows if row["status"] == "UNRESOLVED"]
    resolved = [row for row in rows if row["status"] == "RESOLVED"]
    return {
        "as_of": as_of,
        "read_only": True,
        "mutation_endpoints": 0,
        "incidents": rows,
        "diagnostics": diagnostics,
        "summary": {
            "total": len(rows),
            "unresolved": len(unresolved),
            "resolved": len(resolved),
            "observed": sum(row["status"] == "OBSERVED" for row in rows),
            "acknowledged": sum(row["acknowledged"] for row in rows),
            "critical_unresolved": sum(row["severity"] == "CRITICAL" for row in unresolved),
            "all_critical_visible": all(
                row["visible"] for row in rows if row["severity"] == "CRITICAL"
            ),
        },
    }
