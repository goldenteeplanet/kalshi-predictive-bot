from __future__ import annotations

from collections.abc import Mapping
from typing import Any

VALID_STATES = {
    "RUNNING",
    "WAITING",
    "BLOCKED",
    "PASSED",
    "FAILED",
    "APPROVAL_REQUIRED",
}
EVIDENCE_PASS_PREFIXES = ("PASSED", "VERIFIED", "OK")


def reconcile_phase_roadmap(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize exact roadmap phases without inferring completion.

    A phase may only remain PASSED when it has at least one explicitly passing
    evidence item. A phase that needs approval is kept at APPROVAL_REQUIRED
    until the input explicitly records approval. Dependencies are evaluated
    against the reconciled phase states, so an upstream failure or incomplete
    phase cannot be hidden by a downstream RUNNING/PASSED claim.
    """

    raw_items = payload.get("exact_phase_roadmap", [])
    if not isinstance(raw_items, list):
        raw_items = []
    diagnostics: list[str] = []
    phases: list[dict[str, Any]] = []
    seen: set[str] = set()

    for position, item in enumerate(raw_items, start=1):
        if not isinstance(item, Mapping):
            diagnostics.append(f"PHASE_ROW_INVALID:{position}")
            continue
        phase_id = str(item.get("id") or "").strip()
        if not phase_id or phase_id in seen:
            diagnostics.append(f"PHASE_ID_INVALID:{position}")
            continue
        seen.add(phase_id)
        claimed_state = str(item.get("state") or "WAITING").upper().replace(" ", "_")
        state = claimed_state if claimed_state in VALID_STATES else "BLOCKED"
        if state != claimed_state:
            diagnostics.append(f"PHASE_STATE_INVALID:{phase_id}")
        approval_required = bool(item.get("approval_required", False))
        approved = item.get("approved") is True
        evidence = _evidence(item.get("evidence"))
        dependencies = _strings(item.get("dependencies"))
        if approval_required and not approved and state not in {"FAILED", "PASSED"}:
            state = "APPROVAL_REQUIRED"
        if state == "PASSED" and not any(row["passing"] for row in evidence):
            state = "BLOCKED"
            diagnostics.append(f"PHASE_SUCCESS_WITHOUT_EVIDENCE:{phase_id}")
        phases.append(
            {
                "id": phase_id,
                "label": str(item.get("label") or phase_id),
                "state": state,
                "claimed_state": claimed_state,
                "approval_required": approval_required,
                "approved": approved,
                "dependencies": dependencies,
                "evidence": evidence,
                "blocker": str(item.get("blocker") or ""),
                "position": position,
            }
        )

    by_id = {phase["id"]: phase for phase in phases}
    for phase in phases:
        unresolved = [
            dependency
            for dependency in phase["dependencies"]
            if dependency not in by_id or by_id[dependency]["state"] != "PASSED"
        ]
        phase["unresolved_dependencies"] = unresolved
        if unresolved and phase["state"] in {"RUNNING", "PASSED"}:
            phase["state"] = "BLOCKED"
            phase["blocker"] = "Waiting for: " + ", ".join(unresolved)
            diagnostics.append(f"PHASE_DEPENDENCY_UNRESOLVED:{phase['id']}")

    active = next((phase["id"] for phase in phases if phase["state"] == "RUNNING"), None)
    next_safe = next(
        (
            phase["id"]
            for phase in phases
            if phase["state"] == "WAITING"
            and not phase["unresolved_dependencies"]
            and (not phase["approval_required"] or phase["approved"])
        ),
        None,
    )
    counts = {state: sum(phase["state"] == state for phase in phases) for state in VALID_STATES}
    return {
        "read_only": True,
        "phases": phases,
        "phase_count": len(phases),
        "active_phase": active,
        "next_safe_phase": next_safe,
        "counts": counts,
        "diagnostics": diagnostics,
    }


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or not item.get("path"):
            continue
        status = str(item.get("status") or "UNVERIFIED").upper()
        rows.append(
            {
                "path": str(item["path"]),
                "status": status,
                "sha256": item.get("sha256"),
                "passing": status.startswith(EVIDENCE_PASS_PREFIXES),
            }
        )
    return rows
