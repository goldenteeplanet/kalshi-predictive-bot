from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.phase_prov14b_r2d import (
    WORKFLOW,
    build_ci_workflow_preview,
    write_ci_workflow_preview,
)

ROOT = Path(__file__).parents[1]


def test_repository_workflow_passes_every_offline_safety_check() -> None:
    report = build_ci_workflow_preview(ROOT)
    assert report["status"] == "PASSED"
    assert all(report["checks"].values())
    assert report["guardrails"]["deployment_connected"] is False
    assert report["guardrails"]["runtime_controls"] is False
    assert report["guardrails"]["execution_activation"] is False


def test_workflow_runs_r2c_and_retains_failure_artifact() -> None:
    text = (ROOT / WORKFLOW).read_text(encoding="utf-8")
    assert "python scripts/prov14b_r2c_preview.py" in text
    assert "if: always()" in text
    assert "actions/upload-artifact@v4" in text
    assert "if-no-files-found: error" in text
    assert "retention-days: 30" in text


def test_workflow_has_no_credentials_or_cloud_runtime_commands() -> None:
    text = (ROOT / WORKFLOW).read_text(encoding="utf-8")
    for forbidden in (
        "secrets.",
        "ssh ",
        "scp ",
        "systemctl",
        "sqlite3 ",
        "EXECUTION_ENABLED=true",
    ):
        assert forbidden not in text


def test_missing_workflow_fails_closed(tmp_path: Path) -> None:
    report = build_ci_workflow_preview(tmp_path)
    assert report["status"] == "FAILED"
    assert report["checks"]["workflow_present"] is False


def test_privileged_surface_mutation_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / WORKFLOW
    target.parent.mkdir(parents=True)
    source = (ROOT / WORKFLOW).read_text(encoding="utf-8")
    target.write_text(source + "\n# systemctl restart forbidden\n", encoding="utf-8")
    report = build_ci_workflow_preview(tmp_path)
    assert report["status"] == "FAILED"
    assert report["checks"]["no_privileged_or_runtime_surfaces"] is False


def test_preview_report_is_deterministic_and_atomic(tmp_path: Path) -> None:
    first = write_ci_workflow_preview(ROOT, tmp_path / "first")
    second = write_ci_workflow_preview(ROOT, tmp_path / "second")
    first_payload = json.loads(first.read_text(encoding="utf-8"))
    second_payload = json.loads(second.read_text(encoding="utf-8"))
    assert first_payload == second_payload
    assert not first.with_suffix(".json.tmp").exists()
