from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.phase_evidence_publisher import publish_exact_phase_roadmap
from kalshi_predictor.ui.phase_reconciler import reconcile_phase_roadmap


def write_report(root: Path, relative: str, payload: dict) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_report_never_infers_success(tmp_path: Path) -> None:
    published = publish_exact_phase_roadmap(tmp_path)
    assert all(row["state"] != "PASSED" for row in published["exact_phase_roadmap"])


def test_explicit_pass_is_hashed_and_published(tmp_path: Path) -> None:
    write_report(
        tmp_path,
        "phase_r5_recovery3b/r5_recovery3b_guarded_one_cycle_certification.json",
        {"status": "PASSED"},
    )
    published = publish_exact_phase_roadmap(tmp_path)
    row = published["exact_phase_roadmap"][0]
    assert row["state"] == "PASSED"
    assert len(row["evidence"][0]["sha256"]) == 64


def test_approval_and_runtime_state_are_explicit(tmp_path: Path) -> None:
    published = publish_exact_phase_roadmap(
        tmp_path,
        approvals={"R5-RECOVERY-9": False},
        runtime_states={"PROV-14B": "RUNNING"},
    )
    rows = {row["id"]: row for row in published["exact_phase_roadmap"]}
    assert rows["R5-RECOVERY-9"]["state"] == "APPROVAL_REQUIRED"
    assert rows["PROV-14B"]["state"] == "RUNNING"


def test_reconciler_blocks_runtime_phase_when_dependency_is_unverified(tmp_path: Path) -> None:
    published = publish_exact_phase_roadmap(
        tmp_path,
        approvals={"STORAGE-CAP-2": True},
        runtime_states={"PROV-14B": "RUNNING"},
    )
    reconciled = reconcile_phase_roadmap(published)
    row = next(item for item in reconciled["phases"] if item["id"] == "PROV-14B")
    assert row["state"] == "BLOCKED"
    assert row["unresolved_dependencies"] == ["STORAGE-CAP-2"]
