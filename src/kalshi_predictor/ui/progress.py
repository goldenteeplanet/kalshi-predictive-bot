from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.ui.backup_verification import normalize_backup_verification
from kalshi_predictor.ui.certification_status import build_ci_certification_status
from kalshi_predictor.ui.incident_resolution import (
    acknowledgment_path_for,
    build_incident_resolution_preview,
)
from kalshi_predictor.ui.live_roadmap_status import build_live_roadmap_status
from kalshi_predictor.ui.performance_cache import BoundedSingleFlightCache
from kalshi_predictor.ui.phase_reconciler import reconcile_phase_roadmap
from kalshi_predictor.ui.process_progress import normalize_process_progress
from kalshi_predictor.ui.progress_history import history_path_for, load_progress_timeline
from kalshi_predictor.ui.prov14b_certification_history import (
    load_prov14b_certification_timeline,
)
from kalshi_predictor.ui.prov14b_pipeline_status import normalize_prov14b_pipeline
from kalshi_predictor.ui.roadmap_summary import normalize_roadmap_summary
from kalshi_predictor.ui.timeline_export_status import build_timeline_export_status
from kalshi_predictor.ui.workstream_registry import normalize_workstream_registry

VALID_STATES = {"RUNNING", "WAITING", "BLOCKED", "PASSED", "FAILED"}
DEFAULT_PROGRESS_SNAPSHOT = Path("reports/ui_obs1/progress_snapshot.json")
DEFAULT_CERTIFICATION_REPORTS_ROOT = Path("reports")
MAX_PROGRESS_SNAPSHOT_BYTES = 1_048_576
_PROGRESS_CACHE: BoundedSingleFlightCache[dict[str, Any]] = BoundedSingleFlightCache(
    ttl_seconds=30, max_entries=1
)


def progress_snapshot_path() -> Path:
    return Path(os.environ.get("KALSHI_PROGRESS_SNAPSHOT_PATH", DEFAULT_PROGRESS_SNAPSHOT))


def certification_reports_root() -> Path:
    return Path(
        os.environ.get("KALSHI_CERTIFICATION_REPORTS_ROOT", DEFAULT_CERTIFICATION_REPORTS_ROOT)
    )


def _state(value: Any) -> str:
    normalized = str(value or "BLOCKED").upper()
    return normalized if normalized in VALID_STATES else "BLOCKED"


def build_progress_dashboard(snapshot_path: Path | None = None) -> dict[str, Any]:
    path = snapshot_path or progress_snapshot_path()
    diagnostics: list[str] = []
    try:
        if path.stat().st_size > MAX_PROGRESS_SNAPSHOT_BYTES:
            raise ValueError("STATUS_SNAPSHOT_TOO_LARGE")
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        payload = {}
        diagnostics.append("STATUS_SNAPSHOT_MISSING")
    except json.JSONDecodeError:
        payload = {}
        diagnostics.append("STATUS_SNAPSHOT_INVALID")
    except ValueError as exc:
        payload = {}
        diagnostics.append(str(exc) if str(exc) else "STATUS_SNAPSHOT_INVALID")
    except (OSError, UnicodeDecodeError):
        payload = {}
        diagnostics.append("STATUS_SNAPSHOT_INVALID")
    generated_at = payload.get("generated_at")
    try:
        parsed = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        age_seconds = max(0, int((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()))
    except (TypeError, ValueError):
        age_seconds = None
        diagnostics.append("STATUS_TIMESTAMP_INVALID")
    if age_seconds is not None and age_seconds > 300:
        diagnostics.append("STATUS_SNAPSHOT_STALE")
    reference_time = (
        parsed.astimezone(UTC)
        if "parsed" in locals() and isinstance(parsed, datetime)
        else datetime.now(UTC)
    )
    process, process_diagnostics = normalize_process_progress(
        dict(payload.get("active_process") or {}), reference_time=reference_time
    )
    diagnostics.extend(process_diagnostics)
    if any(
        code in diagnostics
        for code in (
            "STATUS_SNAPSHOT_MISSING",
            "STATUS_SNAPSHOT_INVALID",
            "STATUS_SNAPSHOT_TOO_LARGE",
        )
    ):
        process["state"] = "BLOCKED"
    if process["state"] == "PASSED" and not process["completion_evidence"]:
        process["state"] = "BLOCKED"
        diagnostics.append("PROCESS_SUCCESS_WITHOUT_EVIDENCE")
    execution_enabled = bool(payload.get("execution_enabled", False))
    if execution_enabled:
        diagnostics.append("EXECUTION_ENABLED_CRITICAL")
    alerts = [dict(item) for item in payload.get("alerts", []) if isinstance(item, dict)]
    backup_verification, backup_diagnostics = normalize_backup_verification(
        dict(payload.get("backup_verification") or {})
    )
    diagnostics.extend(backup_diagnostics)
    alerts.extend(
        {
            "severity": "CRITICAL" if "EXECUTION" in code else "WARNING",
            "code": code,
            "message": code.replace("_", " ").title(),
        }
        for code in diagnostics
    )
    registry = normalize_workstream_registry(payload)
    workstreams = registry["workstreams"]
    roadmap_summary = normalize_roadmap_summary(payload)
    diagnostics.extend(roadmap_summary["diagnostics"])
    phase_roadmap = reconcile_phase_roadmap(payload)
    diagnostics.extend(phase_roadmap["diagnostics"])
    live_roadmap = build_live_roadmap_status(payload, reference_time=reference_time)
    diagnostics.extend(live_roadmap["diagnostics"])
    prov14b_pipeline, pipeline_diagnostics = normalize_prov14b_pipeline(
        payload, reference_time=reference_time
    )
    diagnostics.extend(pipeline_diagnostics)
    history_path = history_path_for(path)
    incident_resolution = build_incident_resolution_preview(
        history_path,
        acknowledgment_path_for(history_path),
        as_of=str(generated_at or datetime.now(UTC).isoformat()),
    )
    return {
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "read_only": True,
        "polling": {
            "interval_seconds": int(
                (payload.get("collector") or {}).get("poll_interval_seconds") or 15
            ),
            "timeout_seconds": 5,
            "max_consecutive_failures": 3,
        },
        "execution": {
            "enabled": execution_enabled,
            "paper_enabled": bool(payload.get("paper_enabled", False)),
            "label": "ENABLED — STOP" if execution_enabled else "DISABLED",
        },
        "active_process": process,
        "writer": dict(
            payload.get("writer")
            or {"state": "BLOCKED", "safe_to_start_write": False, "lock_status": "UNKNOWN"}
        ),
        "backup": dict(payload.get("backup") or {"state": "WAITING", "integrity": "UNKNOWN"}),
        "backup_verification": backup_verification,
        "scheduler": dict(payload.get("scheduler") or {"state": "WAITING", "cycle": "unknown"}),
        "reports": list(payload.get("reports") or []),
        "alerts": alerts,
        "workstreams": workstreams,
        "roadmap_summary": roadmap_summary,
        "phase_roadmap": phase_roadmap,
        "live_roadmap": live_roadmap,
        "prov14b_pipeline": prov14b_pipeline,
        "prov14b_timeline": load_prov14b_certification_timeline(history_path),
        "timeline_export": build_timeline_export_status(certification_reports_root()),
        "workstream_registry": {
            "schema_version": registry["schema_version"],
            "coverage": registry["coverage"],
        },
        "diagnostics": diagnostics,
        "timeline": load_progress_timeline(history_path),
        "incident_resolution": incident_resolution,
        "ci_certification": build_ci_certification_status(certification_reports_root()),
    }


def get_cached_progress_dashboard() -> dict[str, Any]:
    path = progress_snapshot_path()
    key = f"{path.resolve()}::{certification_reports_root().resolve()}"
    return _PROGRESS_CACHE.get(key, lambda: build_progress_dashboard(path))


def progress_cache_metrics() -> dict[str, int | float]:
    return _PROGRESS_CACHE.metrics()
