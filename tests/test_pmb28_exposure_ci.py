import json
from pathlib import Path

from kalshi_predictor.benchmarking.exposure_ci import (
    run_offline_exposure_certification_ci_gate,
)

ROOT = Path(__file__).parents[1]
GOLDEN = ROOT / "tests/golden/pmb27_exposure_guard_bundle_summary.json"


def test_pmb28_regenerates_and_passes_exact_golden_gate(tmp_path):
    path, exit_code = run_offline_exposure_certification_ci_gate(
        ROOT, tmp_path / "pass"
    )
    report = json.loads(path.read_text())
    assert exit_code == 0
    assert report["summary"]["passed"] is True
    assert all(report["checks"].values())
    assert report["runtime_activation_authorized"] is False
    assert report["policy_activated"] is False
    assert report["database_writes"] == 0
    assert report["cloud_access"] is False


def test_pmb28_is_deterministic_across_output_directories(tmp_path):
    first, _ = run_offline_exposure_certification_ci_gate(ROOT, tmp_path / "a")
    second, _ = run_offline_exposure_certification_ci_gate(ROOT, tmp_path / "b")
    assert json.loads(first.read_text()) == json.loads(second.read_text())


def test_pmb28_returns_failure_on_golden_drift(tmp_path):
    tampered = json.loads(GOLDEN.read_text())
    tampered["bundle_digest"] = "0" * 64
    bad_golden = tmp_path / "tampered.json"
    bad_golden.write_text(json.dumps(tampered), encoding="utf-8")
    path, exit_code = run_offline_exposure_certification_ci_gate(
        ROOT, tmp_path / "fail", golden_path=bad_golden
    )
    report = json.loads(path.read_text())
    assert exit_code == 1
    assert report["summary"]["passed"] is False
    assert report["checks"]["golden_summary_match"] is False
    assert report["checks"]["bundle_digest_match"] is False
