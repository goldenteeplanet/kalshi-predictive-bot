import json
from pathlib import Path

from kalshi_predictor.benchmarking.ci_failure_matrix import (
    FAILURE_CASES,
    build_offline_ci_failure_mode_matrix,
    write_offline_ci_failure_mode_matrix,
)

ROOT = Path(__file__).parents[1]


def test_pmb30_detects_all_failure_modes_with_exact_codes(tmp_path):
    report = build_offline_ci_failure_mode_matrix(ROOT, tmp_path / "fixtures")
    assert report["summary"]["control_passed"] is True
    assert report["summary"]["all_failures_detected"] is True
    assert report["summary"]["diagnostics_preserved"] is True
    assert {row["case"] for row in report["cases"]} == set(FAILURE_CASES)


def test_pmb30_is_local_preview_only(tmp_path):
    report = build_offline_ci_failure_mode_matrix(ROOT, tmp_path / "fixtures")
    assert report["database_writes"] == 0
    assert report["cloud_access"] is False
    assert report["execution_enabled"] is False
    assert report["thresholds_changed"] is False
    assert report["policy_activated"] is False


def test_pmb30_report_is_deterministic(tmp_path):
    first = json.loads(write_offline_ci_failure_mode_matrix(ROOT, tmp_path / "a").read_text())
    second = json.loads(write_offline_ci_failure_mode_matrix(ROOT, tmp_path / "b").read_text())
    assert first == second
