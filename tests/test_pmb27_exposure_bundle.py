import json
from pathlib import Path

from kalshi_predictor.benchmarking.exposure_bundle import (
    ARTIFACTS,
    build_exposure_guard_certification_bundle,
    golden_exposure_bundle_summary,
    write_exposure_guard_certification_bundle,
)

ROOT = Path(__file__).parents[1]
GOLDEN = Path(__file__).parent / "golden" / "pmb27_exposure_guard_bundle_summary.json"


def test_pmb27_is_deterministic_local_and_does_not_activate(tmp_path):
    first = json.loads(write_exposure_guard_certification_bundle(ROOT, tmp_path / "a").read_text())
    second = json.loads(write_exposure_guard_certification_bundle(ROOT, tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_enabled"] is False
    assert first["thresholds_changed"] is False
    assert first["policy_activated"] is False
    assert first["certification"]["runtime_activation_authorized"] is False


def test_pmb27_hashes_every_artifact_and_passes_cross_report_checks():
    bundle = build_exposure_guard_certification_bundle(ROOT)
    assert bundle["summary"]["artifact_count"] == len(ARTIFACTS)
    assert bundle["certification"]["passed"] is True
    assert bundle["summary"]["checks_passed"] == bundle["summary"]["checks_total"]
    assert all(len(row["sha256"]) == 64 for row in bundle["artifacts"])


def test_pmb27_matches_golden_summary():
    expected = json.loads(GOLDEN.read_text())
    assert golden_exposure_bundle_summary(build_exposure_guard_certification_bundle(ROOT)) == expected
