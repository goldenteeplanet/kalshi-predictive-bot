from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.phase_reconciler import reconcile_phase_roadmap
from kalshi_predictor.ui.progress import build_progress_dashboard


def phase(phase_id: str, state: str, **updates):
    row = {
        "id": phase_id,
        "label": phase_id,
        "state": state,
        "approval_required": False,
        "approved": False,
        "dependencies": [],
        "evidence": [],
    }
    row.update(updates)
    return row


def test_passed_requires_explicit_passing_evidence() -> None:
    result = reconcile_phase_roadmap({"exact_phase_roadmap": [phase("A", "PASSED")]})
    assert result["phases"][0]["state"] == "BLOCKED"
    assert "PHASE_SUCCESS_WITHOUT_EVIDENCE:A" in result["diagnostics"]


def test_approval_and_dependencies_fail_closed() -> None:
    result = reconcile_phase_roadmap(
        {
            "exact_phase_roadmap": [
                phase("A", "RUNNING"),
                phase("B", "WAITING", approval_required=True),
                phase("C", "RUNNING", dependencies=["A"]),
            ]
        }
    )
    by_id = {row["id"]: row for row in result["phases"]}
    assert by_id["B"]["state"] == "APPROVAL_REQUIRED"
    assert by_id["C"]["state"] == "BLOCKED"
    assert by_id["C"]["unresolved_dependencies"] == ["A"]


def test_next_safe_phase_requires_clear_dependencies_and_approval() -> None:
    result = reconcile_phase_roadmap(
        {
            "exact_phase_roadmap": [
                phase(
                    "A",
                    "PASSED",
                    evidence=[{"path": "reports/a.json", "status": "VERIFIED"}],
                ),
                phase("B", "WAITING", dependencies=["A"]),
            ]
        }
    )
    assert result["next_safe_phase"] == "B"


def test_explicit_passed_preview_status_is_valid_evidence() -> None:
    result = reconcile_phase_roadmap(
        {
            "exact_phase_roadmap": [
                phase(
                    "A",
                    "PASSED",
                    evidence=[{"path": "reports/a.json", "status": "PASSED_LOCAL_PREVIEW"}],
                )
            ]
        }
    )
    assert result["phases"][0]["state"] == "PASSED"


def test_progress_dashboard_exposes_exact_phase_reconciliation(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "generated_at": "2099-01-01T00:00:00Z",
                "execution_enabled": False,
                "active_process": {"state": "WAITING", "name": "none"},
                "exact_phase_roadmap": [phase("PROV-14B", "RUNNING")],
            }
        ),
        encoding="utf-8",
    )
    progress = build_progress_dashboard(snapshot)
    assert progress["phase_roadmap"]["active_phase"] == "PROV-14B"
    assert progress["phase_roadmap"]["read_only"] is True
