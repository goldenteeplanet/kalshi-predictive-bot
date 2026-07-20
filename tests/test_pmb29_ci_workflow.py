import json
from pathlib import Path

from kalshi_predictor.benchmarking.ci_workflow_preview import (
    build_ci_workflow_integration_preview,
    write_ci_workflow_integration_preview,
)

ROOT = Path(__file__).parents[1]


def test_pmb29_workflow_is_preview_only_and_passes_guardrails():
    report = build_ci_workflow_integration_preview(ROOT)
    assert report["summary"]["passed"] is True
    assert all(report["checks"].values())
    assert report["runtime_deployment_connected"] is False
    assert report["policy_activation_connected"] is False
    assert report["database_writes"] == 0
    assert report["cloud_access"] is False


def test_pmb29_workflow_runs_gate_and_preserves_artifact_on_failure():
    text = (ROOT / ".github/workflows/pmb28-offline-certification.yml").read_text()
    assert "python scripts/pmb28_certification_ci.py" in text
    assert "if: always()" in text
    assert "actions/upload-artifact@v4" in text
    assert "if-no-files-found: error" in text


def test_pmb29_preview_report_is_deterministic(tmp_path):
    first = json.loads(write_ci_workflow_integration_preview(ROOT, tmp_path / "a").read_text())
    second = json.loads(write_ci_workflow_integration_preview(ROOT, tmp_path / "b").read_text())
    assert first == second
