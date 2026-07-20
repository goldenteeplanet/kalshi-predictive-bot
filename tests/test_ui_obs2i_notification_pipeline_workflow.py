from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.notification_pipeline_workflow import (
    build_workflow_preview,
    write_workflow_preview,
)


ROOT = Path(__file__).resolve().parents[1]


def test_workflow_runs_gate_and_retains_failure_evidence() -> None:
    report = build_workflow_preview(ROOT)
    assert report["status"] == "PASSED"
    assert all(report["checks"].values())
    assert report["deployment_connected"] is False
    assert report["actual_notifications_sent"] == 0
    assert report["database_writes"] == 0
    assert report["cloud_access"] is False


def test_workflow_is_fail_closed_and_has_no_privileged_surfaces() -> None:
    text = (ROOT / report_path()).read_text(encoding="utf-8")
    assert "continue-on-error" not in text
    assert "permissions:\n  contents: read" in text
    assert "secrets." not in text
    assert "if: always()" in text
    assert "if-no-files-found: error" in text


def report_path() -> Path:
    return Path(".github/workflows/ui-obs2h-notification-pipeline.yml")


def test_preview_report_is_deterministic(tmp_path: Path) -> None:
    first = json.loads(write_workflow_preview(ROOT, tmp_path / "a").read_text(encoding="utf-8"))
    second = json.loads(write_workflow_preview(ROOT, tmp_path / "b").read_text(encoding="utf-8"))
    assert first == second


def test_missing_workflow_fails_closed(tmp_path: Path) -> None:
    report = build_workflow_preview(tmp_path)
    assert report["status"] == "FAILED"
    assert report["checks"]["workflow_present"] is False
