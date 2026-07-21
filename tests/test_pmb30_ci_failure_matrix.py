import json
from pathlib import Path

from kalshi_predictor.benchmarking.ci_failure_matrix import (
    FAILURE_CASES,
    build_offline_ci_failure_mode_matrix,
    write_offline_ci_failure_mode_matrix,
)


def test_pmb30_detects_all_failure_modes_with_exact_codes(pmb_release_root: Path, tmp_path: Path):
    report = build_offline_ci_failure_mode_matrix(pmb_release_root, tmp_path / "fixtures")
    assert report["summary"]["control_passed"] is True
    assert report["summary"]["all_failures_detected"] is True
    assert report["summary"]["diagnostics_preserved"] is True
    assert {row["case"] for row in report["cases"]} == set(FAILURE_CASES)


def test_pmb30_is_local_preview_only(pmb_release_root: Path, tmp_path: Path):
    report = build_offline_ci_failure_mode_matrix(pmb_release_root, tmp_path / "fixtures")
    assert report["database_writes"] == 0
    assert report["cloud_access"] is False
    assert report["execution_enabled"] is False
    assert report["thresholds_changed"] is False
    assert report["policy_activated"] is False


def test_pmb30_report_is_deterministic(pmb_release_root: Path, tmp_path: Path):
    first = json.loads(
        write_offline_ci_failure_mode_matrix(pmb_release_root, tmp_path / "a").read_text()
    )
    second = json.loads(
        write_offline_ci_failure_mode_matrix(pmb_release_root, tmp_path / "b").read_text()
    )
    assert first == second
