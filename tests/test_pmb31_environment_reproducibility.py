import json
from pathlib import Path

from kalshi_predictor.benchmarking.environment_reproducibility import (
    HASH_SEEDS,
    SUPPORTED_PYTHON_VERSIONS,
    write_cross_environment_reproducibility_preview,
)

ROOT = Path(__file__).parents[1]


def test_pmb31_clean_environment_runs_are_reproducible(tmp_path):
    path = write_cross_environment_reproducibility_preview(ROOT, tmp_path)
    report = json.loads(path.read_text())
    assert report["summary"]["local_run_count"] == len(HASH_SEEDS)
    assert report["summary"]["all_local_runs_passed"] is True
    assert report["summary"]["bundle_digest_reproducible"] is True
    assert report["summary"]["summary_digest_reproducible"] is True


def test_pmb31_ci_matrix_covers_supported_python_versions(tmp_path):
    report = json.loads(
        write_cross_environment_reproducibility_preview(ROOT, tmp_path).read_text()
    )
    assert report["supported_python_versions"] == list(SUPPORTED_PYTHON_VERSIONS)
    assert report["summary"]["ci_matrix_covers_supported_versions"] is True
    assert report["database_writes"] == 0
    assert report["cloud_access"] is False
    assert report["execution_enabled"] is False
