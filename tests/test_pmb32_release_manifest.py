import hashlib
import json
from pathlib import Path

from kalshi_predictor.benchmarking.release_manifest import (
    COMMANDS,
    GOLDENS,
    REPORTS,
    build_benchmark_release_manifest,
    write_benchmark_release_manifest,
)


def test_pmb32_manifest_hashes_all_release_evidence(pmb_release_root: Path):
    manifest = build_benchmark_release_manifest(pmb_release_root)
    assert manifest["summary"]["report_count"] == len(REPORTS)
    assert manifest["summary"]["golden_count"] == len(GOLDENS)
    assert manifest["summary"]["command_count"] == len(COMMANDS)
    for row in manifest["files"]:
        assert (
            hashlib.sha256((pmb_release_root / row["path"]).read_bytes()).hexdigest()
            == row["sha256"]
        )


def test_pmb32_certification_stays_preview_only(pmb_release_root: Path):
    manifest = build_benchmark_release_manifest(pmb_release_root)
    assert manifest["certification"]["pmb27_certification_passed"] is True
    assert manifest["certification"]["pmb28_ci_gate_passed"] is True
    assert manifest["certification"]["runtime_activation_authorized"] is False
    assert manifest["database_writes"] == 0
    assert manifest["cloud_access"] is False
    assert manifest["policy_activated"] is False


def test_pmb32_writes_deterministic_manifest_and_checksums(pmb_release_root: Path, tmp_path: Path):
    first = write_benchmark_release_manifest(pmb_release_root, tmp_path / "a")
    second = write_benchmark_release_manifest(pmb_release_root, tmp_path / "b")
    assert json.loads(first.read_text()) == json.loads(second.read_text())
    assert (tmp_path / "a/pmb32_SHA256SUMS.txt").read_text() == (
        tmp_path / "b/pmb32_SHA256SUMS.txt"
    ).read_text()
