from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

REGISTRY_PATH = Path(__file__).with_name("workstream_registry.json")
VALID_STATES = {"RUNNING", "WAITING", "BLOCKED", "PASSED", "FAILED"}


def load_workstream_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("workstreams")
    if not isinstance(rows, list) or not rows:
        raise ValueError("WORKSTREAM_REGISTRY_EMPTY")
    ids = [str(row.get("id") or "") for row in rows]
    names = [str(row.get("name") or "") for row in rows]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("WORKSTREAM_REGISTRY_IDS_INVALID")
    if any(not item for item in names) or len(names) != len(set(names)):
        raise ValueError("WORKSTREAM_REGISTRY_NAMES_INVALID")
    prefixes = [prefix for row in rows for prefix in (row.get("phase_prefixes") or [])]
    if len(prefixes) != len(set(prefixes)) or any(not str(prefix).strip() for prefix in prefixes):
        raise ValueError("WORKSTREAM_REGISTRY_PREFIXES_INVALID")
    return payload


def _key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _state(value: Any) -> str:
    state = str(value or "WAITING").upper()
    return state if state in VALID_STATES else "BLOCKED"


def _source_row(definition: Mapping[str, Any], payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    source = definition.get("source")
    if source in {"backup", "scheduler"}:
        row = payload.get(str(source))
        return row if isinstance(row, Mapping) else None
    aliases = {_key(definition.get("id")), _key(definition.get("name"))}
    aliases.update(_key(alias) for alias in definition.get("aliases") or [])
    for row in payload.get("workstreams") or []:
        if isinstance(row, Mapping) and (_key(row.get("id")) in aliases or _key(row.get("name")) in aliases):
            return row
    return None


def normalize_workstream_registry(payload: Mapping[str, Any]) -> dict[str, Any]:
    registry = load_workstream_registry()
    workstreams = []
    reported = 0
    for definition in registry["workstreams"]:
        source = _source_row(definition, payload)
        is_reported = source is not None
        if is_reported:
            reported += 1
        source = source or {}
        source_kind = definition.get("source")
        current_phase = source.get("current_phase")
        next_safe = source.get("next_safe_phase")
        if source_kind == "backup":
            current_phase = source.get("phase") or ("Backup integrity verified" if source.get("integrity") == "ok" else "Backup status unavailable")
            next_safe = source.get("next_safe_phase") or "Retain verified rollback backup"
        elif source_kind == "scheduler":
            current_phase = source.get("phase") or f"Cycle {source.get('cycle') or 'unknown'} · {source.get('stage') or 'stage unknown'}"
            next_safe = source.get("next_safe_phase") or "Observe the next certified scheduler transition"
        workstreams.append({"id":definition["id"],"name":definition["name"],"state":_state(source.get("state")) if source else "WAITING","current_phase":current_phase or "No active phase reported","completed":list(source.get("completed") or []),"blocked":list(source.get("blocked") or []),"next_safe_phase":next_safe or "Await a certified status update","reported":is_reported,"phase_prefixes":list(definition.get("phase_prefixes") or [])})
    required = len(workstreams)
    return {"schema_version":registry["schema_version"],"workstreams":workstreams,"coverage":{"required":required,"reported":reported,"missing":required-reported,"complete":reported==required}}
