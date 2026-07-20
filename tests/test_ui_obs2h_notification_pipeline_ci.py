from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.notification_pipeline_ci import STAGES, run_notification_pipeline_ci


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
GOLDEN = ROOT / "tests/golden/ui_obs2h_notification_pipeline_golden.json"


def _copy_pipeline(tmp_path: Path) -> Path:
    target = tmp_path / "reports"
    for relative in STAGES.values():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((REPORTS / relative).read_bytes())
    return target


def test_current_pipeline_matches_golden_without_side_effects() -> None:
    result = run_notification_pipeline_ci(REPORTS, GOLDEN)
    assert result["status"] == "PASSED"
    assert result["exit_code"] == 0
    assert result["golden_match"] is True
    assert result["actual_notifications_sent"] == 0
    assert result["network_access"] is False
    assert result["cloud_access"] is False
    assert result["database_writes"] == 0
    assert all(result["bundle"]["cross_stage_checks"].values())


def test_missing_stage_fails_closed(tmp_path: Path) -> None:
    reports = _copy_pipeline(tmp_path)
    (reports / STAGES["UI-OBS-2G"]).unlink()
    result = run_notification_pipeline_ci(reports, GOLDEN)
    assert result["exit_code"] == 1
    assert any(item.startswith("ARTIFACT_MISSING:UI-OBS-2G") for item in result["diagnostics"])


def test_artifact_drift_is_detected(tmp_path: Path) -> None:
    reports = _copy_pipeline(tmp_path)
    path = reports / STAGES["UI-OBS-2D"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["preview"]["generated_at"] = "2099-01-01T00:00:00Z"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = run_notification_pipeline_ci(reports, GOLDEN)
    assert result["exit_code"] == 1
    assert "GOLDEN_DRIFT_DETECTED" in result["diagnostics"]


def test_semantic_break_fails_cross_stage_gate(tmp_path: Path) -> None:
    reports = _copy_pipeline(tmp_path)
    path = reports / STAGES["UI-OBS-2G"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["summary"]["critical_coverage_complete"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = run_notification_pipeline_ci(reports, GOLDEN)
    assert result["exit_code"] == 1
    assert "CROSS_STAGE_CHECK_FAILED:critical_coverage_complete" in result["diagnostics"]


def test_invalid_golden_fails_closed(tmp_path: Path) -> None:
    invalid = tmp_path / "golden.json"
    invalid.write_text("not-json", encoding="utf-8")
    result = run_notification_pipeline_ci(REPORTS, invalid)
    assert result["exit_code"] == 1
    assert "GOLDEN_MANIFEST_MISSING_OR_INVALID" in result["diagnostics"]


def test_gate_is_deterministic() -> None:
    first = run_notification_pipeline_ci(REPORTS, GOLDEN)
    second = run_notification_pipeline_ci(REPORTS, GOLDEN)
    assert first == second
