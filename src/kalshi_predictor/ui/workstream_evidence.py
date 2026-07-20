from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


STREAMS = {
    "pmb": ("PMB evaluation", ("phase_pmb",)),
    "prov": ("PROV attribution", ("phase_prov",)),
    "nyc_weather": ("NYC weather", ("phase_nyc",)),
    "gh_liquidity": ("GH liquidity", ("phase_gh1",)),
    "readiness": ("Paper readiness", ("readiness", "phase_readiness")),
}
MAX_REPORT_BYTES = 1_048_576


def _phase_from(payload: Mapping[str, Any], path: Path, stream: str) -> str | None:
    explicit = payload.get("phase")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().upper()
    text = path.as_posix().lower()
    patterns = {
        "pmb": (r"phase_pmb(\d+[a-z]?)", "PMB-"),
        "prov": (r"phase_prov(\d+[a-z]?)", "PROV-"),
        "nyc_weather": (r"phase_nyc_w(\d+[a-z]?)", "NYC-W"),
        "gh_liquidity": (r"phase_gh1([a-z])", "GH-1"),
        "readiness": (r"readiness[_-]?(\d+)?", "READINESS-"),
    }
    pattern, prefix = patterns[stream]
    match = re.search(pattern, text)
    if not match:
        return None
    suffix = match.group(1) or "1"
    return prefix + suffix.upper()


def _walk_values(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value[:100]:
            yield from _walk_values(child)


def _state(payload: Mapping[str, Any]) -> str:
    for key in ("status", "certification_status", "decision"):
        raw = payload.get(key)
        if isinstance(raw, str):
            normalized = raw.upper().replace(" ", "_")
            if normalized in {"PASSED", "PASS", "COMPLETE", "COMPLETED", "READY", "CERTIFIED"}:
                return "PASSED"
            if "FAIL" in normalized:
                return "FAILED"
            if "BLOCK" in normalized or "NOT_READY" in normalized:
                return "BLOCKED"
            if "RUN" in normalized or "IN_PROGRESS" in normalized:
                return "RUNNING"
            if "WAIT" in normalized or "PENDING" in normalized:
                return "WAITING"
    booleans = {key: value for key, value in _walk_values(payload) if isinstance(value, bool)}
    for key in ("certification_passed", "verification_passed", "multi_window_complete", "runtime_activation_ready"):
        if key in booleans:
            return "PASSED" if booleans[key] else "BLOCKED"
    return "WAITING"


def _next_action(payload: Mapping[str, Any]) -> str:
    for key in ("next_phase", "recommended_next_phase", "next_safe_phase", "next_action"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return "Review the linked evidence before advancing"


def _candidate_paths(root: Path, tokens: tuple[str, ...], *, limit: int) -> list[Path]:
    candidates: list[Path] = []
    for path in root.glob("*/*.json"):
        lowered = path.as_posix().lower()
        if any(token in lowered for token in tokens):
            try:
                if path.stat().st_size <= MAX_REPORT_BYTES:
                    candidates.append(path)
            except OSError:
                continue
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def discover_workstream_evidence(reports_root: Path, *, max_files_per_stream: int = 80) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for stream, (name, tokens) in STREAMS.items():
        selected = None
        for path in _candidate_paths(reports_root, tokens, limit=max_files_per_stream):
            try:
                raw = path.read_bytes()
                payload = json.loads(raw)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            phase = _phase_from(payload, path, stream)
            if not phase:
                continue
            selected = (path, raw, payload, phase)
            break
        if selected is None:
            diagnostics.append(f"WORKSTREAM_EVIDENCE_MISSING:{stream}")
            continue
        path, raw, payload, phase = selected
        state = _state(payload)
        relative = path.relative_to(reports_root).as_posix()
        sha = hashlib.sha256(raw).hexdigest()
        row = {
            "id": stream, "name": name, "state": state, "current_phase": phase,
            "completed": [phase] if state == "PASSED" else [],
            "blocked": [phase] if state in {"BLOCKED", "FAILED"} else [],
            "next_safe_phase": _next_action(payload), "evidence_path": f"reports/{relative}",
            "evidence_sha256": sha, "reported": True,
        }
        rows.append(row)
        reports.append({
            "phase": phase, "state": state, "path": f"reports/{relative}",
            "sha256": sha, "verified": True,
            "generated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat().replace("+00:00", "Z"),
        })
    return {
        "workstreams": rows, "reports": reports,
        "diagnostics": diagnostics, "files_scanned_limit": max_files_per_stream * len(STREAMS),
    }
