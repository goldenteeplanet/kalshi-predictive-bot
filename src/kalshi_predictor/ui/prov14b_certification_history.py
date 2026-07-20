"""Build a bounded, read-only PROV-14B transition timeline."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_EVENT_LIMIT = 40
GATE_IDS = ("R2A", "R2B", "R2C", "R2D")
MAX_HISTORY_BYTES = 4_194_304


def load_prov14b_certification_timeline(
    history_path: Path, *, event_limit: int = DEFAULT_EVENT_LIMIT
) -> dict[str, Any]:
    if not 4 <= event_limit <= 200:
        raise ValueError("event limit must be between 4 and 200")
    diagnostics: list[str] = []
    try:
        if history_path.stat().st_size > MAX_HISTORY_BYTES:
            raise ValueError("PROV14B_HISTORY_TOO_LARGE")
        payload = json.loads(history_path.read_text(encoding="utf-8"))
        raw_entries = payload.get("entries")
    except FileNotFoundError:
        raw_entries = []
    except (OSError, ValueError, json.JSONDecodeError):
        raw_entries = []
        diagnostics.append("PROV14B_HISTORY_UNREADABLE")
    if not isinstance(raw_entries, list):
        raw_entries = []
        diagnostics.append("PROV14B_HISTORY_ENTRIES_INVALID")

    entries = _valid_entries(raw_entries, diagnostics)
    events: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for entry in entries:
        events.extend(_transitions(previous, entry))
        previous = entry
    duration_seconds, duration_state = _duration(entries)
    current = entries[-1].get("prov14b", {}) if entries else {}
    return {
        "reported": bool(entries),
        "state": str(current.get("state") or "WAITING"),
        "current_stage": str(current.get("current_stage") or "not reported"),
        "duration_seconds": duration_seconds,
        "duration_state": duration_state,
        "events": list(reversed(events[-event_limit:])),
        "event_count": min(len(events), event_limit),
        "retention_limit": event_limit,
        "diagnostics": diagnostics,
        "read_only": True,
        "controls_available": False,
    }


def _valid_entries(raw_entries: list[Any], diagnostics: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in raw_entries[-1000:]:
        if not isinstance(item, dict) or not isinstance(item.get("prov14b"), dict):
            continue
        try:
            timestamp = _parse(item.get("generated_at"))
        except (TypeError, ValueError):
            diagnostics.append("PROV14B_HISTORY_TIMESTAMP_INVALID")
            continue
        row = dict(item)
        row["_timestamp"] = timestamp
        result.append(row)
    result.sort(key=lambda row: row["_timestamp"])
    return result


def _transitions(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    now = current["_timestamp"].isoformat()
    current_pipeline = current["prov14b"]
    before_pipeline = previous.get("prov14b", {}) if previous else {}
    events: list[dict[str, Any]] = []
    before_gates = before_pipeline.get("gates", {})
    current_gates = current_pipeline.get("gates", {})
    for gate_id in GATE_IDS:
        before = before_gates.get(gate_id, {})
        after = current_gates.get(gate_id, {})
        before_state = before.get("state")
        after_state = after.get("state")
        if after_state and before_state != after_state:
            events.append(_event(now, gate_id, "GATE_TRANSITION", before_state, after_state))
        before_fresh = _freshness(before)
        after_fresh = _freshness(after)
        if after and before_fresh != after_fresh:
            events.append(_event(now, gate_id, "FRESHNESS_CHANGED", before_fresh, after_fresh))
    before_alerts = set(before_pipeline.get("alerts") or [])
    after_alerts = set(current_pipeline.get("alerts") or [])
    for code in sorted(after_alerts - before_alerts):
        events.append(_event(now, "PIPELINE", "ALERT_OPENED", None, code))
    for code in sorted(before_alerts - after_alerts):
        events.append(_event(now, "PIPELINE", "ALERT_RESOLVED", code, "RESOLVED"))
    return events


def _event(
    timestamp: str, subject: str, event_type: str, before: Any, after: Any
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "subject": subject,
        "event_type": event_type,
        "before": before,
        "after": after,
        "resolved": event_type == "ALERT_RESOLVED",
    }


def _freshness(gate: dict[str, Any]) -> str | None:
    if not gate:
        return None
    if gate.get("evidence_age_seconds") is None:
        return "MISSING"
    return "STALE" if gate.get("evidence_stale") else "FRESH"


def _duration(entries: list[dict[str, Any]]) -> tuple[int | None, str]:
    if not entries:
        return None, "UNAVAILABLE"
    started = entries[0]["_timestamp"]
    passed = next(
        (row["_timestamp"] for row in entries if row["prov14b"].get("state") == "PASSED"),
        None,
    )
    ended = passed or entries[-1]["_timestamp"]
    seconds = max(0, int((ended - started).total_seconds()))
    return seconds, "CERTIFIED" if passed else "IN_PROGRESS"


def _parse(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed
