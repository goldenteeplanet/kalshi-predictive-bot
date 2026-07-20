from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LANES = (
    ("r5", "R5 scheduler recovery"),
    ("provenance", "Forecast provenance"),
    ("liquidity", "Liquidity and executable edge"),
    ("weather", "NYC weather observation"),
    ("readiness", "Paper readiness"),
)
VALID_STATES = {"RUNNING", "WAITING", "BLOCKED", "PASSED", "FAILED"}


def normalize_roadmap_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_lanes = {
        str(item.get("id")): dict(item)
        for item in payload.get("roadmap_summary", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    lanes: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for lane_id, name in LANES:
        raw = raw_lanes.get(lane_id, {})
        state = str(raw.get("state") or "BLOCKED").upper()
        if state not in VALID_STATES:
            state = "BLOCKED"
            diagnostics.append(f"ROADMAP_STATE_INVALID:{lane_id}")
        evidence = [
            dict(item)
            for item in raw.get("evidence", [])
            if isinstance(item, Mapping) and item.get("path")
        ][:5]
        if state == "PASSED" and not evidence:
            state = "BLOCKED"
            diagnostics.append(f"ROADMAP_SUCCESS_WITHOUT_EVIDENCE:{lane_id}")
        lanes.append(
            {
                "id": lane_id,
                "name": name,
                "state": state,
                "current_phase": str(raw.get("current_phase") or "UNREPORTED"),
                "progress_label": str(raw.get("progress_label") or "No current evidence"),
                "blocker": str(raw.get("blocker") or "Status has not been reported"),
                "next_phase": str(raw.get("next_phase") or "Evidence required"),
                "metrics": _metrics(raw.get("metrics")),
                "evidence": evidence,
                "reported": bool(raw),
            }
        )
    states = {lane["state"] for lane in lanes}
    overall = next(
        (
            state
            for state in ("FAILED", "RUNNING", "BLOCKED", "WAITING", "PASSED")
            if state in states
        ),
        "BLOCKED",
    )
    return {
        "state": overall,
        "read_only": True,
        "lanes": lanes,
        "reported_lanes": sum(lane["reported"] for lane in lanes),
        "required_lanes": len(LANES),
        "diagnostics": diagnostics,
    }


def _metrics(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Mapping):
        return []
    return [
        {"label": str(key).replace("_", " ").title(), "value": str(item)}
        for key, item in sorted(value.items())
    ][:6]
