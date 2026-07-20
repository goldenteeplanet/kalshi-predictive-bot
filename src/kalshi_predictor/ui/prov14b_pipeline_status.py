"""Normalize read-only PROV-14B backup and R2A-R2D dashboard evidence."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

VALID_STATES = {"RUNNING", "WAITING", "BLOCKED", "PASSED", "FAILED"}
STAGE_IDS = ("backup_copy", "quick_check", "sha256", "integrity_check")
GATE_IDS = ("R2A", "R2B", "R2C", "R2D")
GATE_MAX_AGE_SECONDS = 3600
_SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def normalize_prov14b_pipeline(
    payload: dict[str, Any], *, reference_time: datetime
) -> tuple[dict[str, Any], list[str]]:
    raw = payload.get("prov14b_certification_pipeline")
    if not isinstance(raw, dict):
        return _unreported(), []
    diagnostics: list[str] = []
    captured_at, age_seconds = _capture_time(raw.get("captured_at"), reference_time)
    if captured_at is None:
        diagnostics.append("PROV14B_PIPELINE_TIMESTAMP_INVALID")
    stale = age_seconds is None or age_seconds > 300
    if stale:
        diagnostics.append("PROV14B_PIPELINE_EVIDENCE_STALE")

    raw_stages = raw.get("backup_stages") if isinstance(raw.get("backup_stages"), dict) else {}
    stages = []
    for stage_id in STAGE_IDS:
        item = raw_stages.get(stage_id)
        item = item if isinstance(item, dict) else {}
        state = _state(item.get("state"), default="WAITING")
        if state == "PASSED" and not item.get("evidence"):
            state = "BLOCKED"
            diagnostics.append(f"PROV14B_{stage_id.upper()}_PASS_WITHOUT_EVIDENCE")
        stages.append({
            "id": stage_id,
            "label": stage_id.replace("_", " ").title(),
            "state": state,
            "evidence": str(item.get("evidence") or ""),
            "detail": str(item.get("detail") or ""),
        })

    raw_gates = raw.get("gates") if isinstance(raw.get("gates"), dict) else {}
    gates = []
    alerts: list[dict[str, str]] = []
    for gate_id in GATE_IDS:
        item = raw_gates.get(gate_id)
        item = item if isinstance(item, dict) else {}
        state = _state(item.get("state"), default="WAITING")
        sha = str(item.get("report_sha256") or "")
        failed_count = _nonnegative_int(item.get("failed_count"))
        generated_at, evidence_age_seconds = _capture_time(
            item.get("generated_at"), reference_time
        )
        artifact_id = str(item.get("artifact_id") or "")
        artifact_valid = bool(_SAFE_ARTIFACT_ID.fullmatch(artifact_id))
        evidence_stale = (
            evidence_age_seconds is None or evidence_age_seconds > GATE_MAX_AGE_SECONDS
        )
        evidence_valid = (
            len(sha) == 64
            and failed_count == 0
            and artifact_valid
            and not evidence_stale
        )
        if state == "PASSED" and not evidence_valid:
            state = "BLOCKED"
            diagnostics.append(f"PROV14B_{gate_id}_PASS_WITHOUT_EXACT_EVIDENCE")
        if generated_at is None:
            alerts.append(_alert(gate_id, "MISSING_TIMESTAMP", "Evidence timestamp missing"))
        elif evidence_stale:
            alerts.append(_alert(gate_id, "STALE_EVIDENCE", "Evidence exceeds 60 minutes"))
        if not artifact_valid:
            alerts.append(_alert(gate_id, "MISSING_ARTIFACT", "Safe local artifact link missing"))
        details = _evidence_details(item.get("evidence_details"), gate_id, diagnostics)
        gates.append({
            "id": gate_id,
            "label": f"PROV-14B-{gate_id}",
            "state": state,
            "report_sha256": sha or None,
            "failed_count": failed_count,
            "runtime_certified": item.get("runtime_certified") is True,
            "detail": str(item.get("detail") or ""),
            "generated_at": generated_at,
            "evidence_age_seconds": evidence_age_seconds,
            "evidence_stale": evidence_stale,
            "artifact_id": artifact_id if artifact_valid else None,
            "artifact_href": f"/system/evidence/{artifact_id}" if artifact_valid else None,
            "evidence_details": details,
        })

    all_backup_passed = all(item["state"] == "PASSED" for item in stages)
    all_gates_passed = all(item["state"] == "PASSED" for item in gates)
    any_failed = any(item["state"] == "FAILED" for item in [*stages, *gates])
    any_running = any(item["state"] == "RUNNING" for item in [*stages, *gates])
    if stale:
        state = "BLOCKED"
    elif any_failed:
        state = "FAILED"
    elif all_backup_passed and all_gates_passed:
        state = "PASSED"
    elif any_running:
        state = "RUNNING"
    else:
        state = "WAITING"
    current_stage = str(raw.get("current_stage") or "unknown")
    return {
        "reported": True,
        "state": state,
        "captured_at": captured_at,
        "age_seconds": age_seconds,
        "stale": stale,
        "current_stage": current_stage,
        "backup_stages": stages,
        "gates": gates,
        "deployment_blocked": not (all_backup_passed and all_gates_passed) or stale,
        "runtime_certified": any(
            item["id"] == "R2A" and item["runtime_certified"] for item in gates
        ),
        "read_only": True,
        "controls_available": False,
        "alerts": alerts,
        "alert_count": len(alerts),
    }, diagnostics


def _unreported() -> dict[str, Any]:
    return {
        "reported": False,
        "state": "WAITING",
        "captured_at": None,
        "age_seconds": None,
        "stale": False,
        "current_stage": "not reported",
        "backup_stages": [
            {
                "id": stage_id,
                "label": stage_id.replace("_", " ").title(),
                "state": "WAITING",
                "evidence": "",
                "detail": "",
            }
            for stage_id in STAGE_IDS
        ],
        "gates": [
            {
                "id": gate_id,
                "label": f"PROV-14B-{gate_id}",
                "state": "WAITING",
                "report_sha256": None,
                "failed_count": None,
                "runtime_certified": False,
                "detail": "",
                "generated_at": None,
                "evidence_age_seconds": None,
                "evidence_stale": True,
                "artifact_id": None,
                "artifact_href": None,
                "evidence_details": [],
            }
            for gate_id in GATE_IDS
        ],
        "deployment_blocked": True,
        "runtime_certified": False,
        "read_only": True,
        "controls_available": False,
        "alerts": [],
        "alert_count": 0,
    }


def _alert(gate_id: str, code: str, message: str) -> dict[str, str]:
    return {
        "severity": "CRITICAL" if code != "STALE_EVIDENCE" else "WARNING",
        "gate": gate_id,
        "code": f"PROV14B_{gate_id}_{code}",
        "message": message,
    }


def _evidence_details(
    value: Any, gate_id: str, diagnostics: list[str]
) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        diagnostics.append(f"PROV14B_{gate_id}_EVIDENCE_DETAILS_INVALID")
        return []
    result: list[dict[str, str]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            diagnostics.append(f"PROV14B_{gate_id}_EVIDENCE_DETAIL_INVALID")
            continue
        label = str(item.get("label") or "").strip()[:80]
        detail = str(item.get("value") or "").strip()[:200]
        if label and detail:
            result.append({"label": label, "value": detail})
    return result


def _capture_time(value: Any, reference_time: datetime) -> tuple[str | None, int | None]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None, None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, None
    age = int((reference_time.astimezone(UTC) - parsed.astimezone(UTC)).total_seconds())
    if age < 0:
        return None, None
    return parsed.astimezone(UTC).isoformat(), age


def _state(value: Any, *, default: str) -> str:
    normalized = str(value or default).upper()
    return normalized if normalized in VALID_STATES else "BLOCKED"


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
