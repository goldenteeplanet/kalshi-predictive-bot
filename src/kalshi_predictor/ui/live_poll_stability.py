from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


REQUIRED_WORKSTREAMS = {"pmb", "prov", "nyc_weather", "gh_liquidity", "readiness"}


def certify_live_poll_stability(snapshots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    previous: datetime | None = None
    for index, snapshot in enumerate(snapshots, 1):
        timestamp = _time(snapshot.get("generated_at"))
        if timestamp is None:
            failures.append(f"POLL_{index}_TIMESTAMP_INVALID")
        interval = None if previous is None or timestamp is None else (timestamp - previous).total_seconds()
        if interval is not None and not 15 <= interval <= 60:
            failures.append(f"POLL_{index}_INTERVAL_OUT_OF_BOUNDS")
        if timestamp is not None:
            previous = timestamp
        scheduler = snapshot.get("scheduler") if isinstance(snapshot.get("scheduler"), Mapping) else {}
        workstreams = snapshot.get("workstreams") if isinstance(snapshot.get("workstreams"), list) else []
        workstream_ids = {row.get("id") for row in workstreams if isinstance(row, Mapping)}
        roadmap = snapshot.get("phase_roadmap") if isinstance(snapshot.get("phase_roadmap"), list) else []
        certification = snapshot.get("r5_recovery9_certification") if isinstance(snapshot.get("r5_recovery9_certification"), Mapping) else {}
        prov14b = snapshot.get("prov14b") if isinstance(snapshot.get("prov14b"), Mapping) else {}
        gates = {
            "execution_disabled": snapshot.get("execution_enabled") is False,
            "read_only": (snapshot.get("collector") or {}).get("read_only") is True,
            "database_writes_zero": (snapshot.get("collector") or {}).get("database_writes") == 0,
            "bounded_service": scheduler.get("service") == "kalshi-r5-bounded.service",
            "bounded_timer": scheduler.get("timer") == "kalshi-r5-bounded.timer",
            "legacy_disabled": scheduler.get("legacy_watcher_enabled") is False and scheduler.get("legacy_watcher_active") is False,
            "roadmap_complete": len(roadmap) == 20,
            "workstreams_complete": REQUIRED_WORKSTREAMS <= workstream_ids,
            "r5_certified": certification.get("status") == "PASSED" and certification.get("rollback_verified") is True,
            "prov14b_visible": str(prov14b.get("state", "")).upper() in {"QUEUED", "WAITING", "RUNNING", "PASSED"},
        }
        for gate, passed in gates.items():
            if not passed:
                failures.append(f"POLL_{index}_{gate.upper()}_FAILED")
        rows.append({
            "poll": index, "generated_at": snapshot.get("generated_at"),
            "interval_from_previous_seconds": interval, "gates": gates,
        })
    if len(snapshots) < 3:
        failures.append("INSUFFICIENT_DISTINCT_POLLS")
    timestamps = [row["generated_at"] for row in rows]
    if len(set(timestamps)) != len(timestamps):
        failures.append("DUPLICATE_POLL_TIMESTAMPS")
    return {
        "phase": "UI-OBS-5G",
        "mode": "READ_ONLY_MULTI_POLL_LIVE_STABILITY_CENSUS",
        "status": "PASSED" if not failures else "FAILED",
        "polls_required": 3, "polls_observed": len(rows), "polls": rows,
        "failures": sorted(set(failures)), "cloud_writes": 0, "database_writes": 0,
        "service_controls": 0, "execution_enabled": False,
    }


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None
