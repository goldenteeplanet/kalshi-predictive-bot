from __future__ import annotations

import copy
import subprocess
import tempfile
from pathlib import Path

from scripts.local.phase4mj_recovery_workstream_certification import (
    PHASE_META,
    certify_workstream,
    make_test_evidence,
)
from scripts.local.phase4mk_clean_checkout_reproducibility import (
    audit_clean_checkout,
    compare_certifications,
    inventory_from_certification,
    normalize_certification,
    verify_export_inventory,
)

ROOT = Path(__file__).resolve().parents[1]
RANGE_HEAD = "1d7e9cf073ec0fcb2ff1662b6ee0e208fc83aac0"


def _head():
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, check=True, text=True
    ).stdout.strip()


def _evidence():
    return make_test_evidence(
        phases=list(PHASE_META),
        test_count=378,
        command="full phase4lk-phase4mj workstream tests",
        result_summary="378 passed",
    )


def test_inventory_verifier_accepts_current_certified_files() -> None:
    certification = certify_workstream(ROOT, _evidence(), expected_head=RANGE_HEAD)
    inventory = inventory_from_certification(certification)
    result = verify_export_inventory(ROOT, inventory)
    assert result["verdict"] == "PASS"
    assert result["verified_count"] == 75


def test_inventory_verifier_refuses_missing_corrupt_and_untracked_substitution() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "docs").mkdir()
        path = root / "docs/phase4lk-runtime-configuration-snapshot.md"
        path.write_text("expected", encoding="utf-8")
        expected = [
            {
                "path": path.relative_to(root).as_posix(),
                "content_sha256": __import__("hashlib").sha256(b"expected").hexdigest(),
            }
        ]
        assert verify_export_inventory(root, expected)["verdict"] == "PASS"
        path.write_text("corrupt", encoding="utf-8")
        assert verify_export_inventory(root, expected)["verdict"] == "REFUSE"
        path.unlink()
        assert any(
            "MISSING_BLOB" in error for error in verify_export_inventory(root, expected)["errors"]
        )
        path.write_text("expected", encoding="utf-8")
        extra = root / "docs/phase4ll-untracked.md"
        extra.write_text("substitution", encoding="utf-8")
        assert any(
            "UNTRACKED_SUBSTITUTION" in error
            for error in verify_export_inventory(root, expected)["errors"]
        )


def test_certification_normalization_ignores_only_environment_fields() -> None:
    primary = certify_workstream(ROOT, _evidence(), expected_head=RANGE_HEAD)
    isolated = copy.deepcopy(primary)
    isolated["repository"] = "/different/checkout"
    isolated["repository_head_commit"] = "f" * 40
    isolated["certification_sha256"] = "f" * 64
    assert normalize_certification(primary) == normalize_certification(isolated)
    assert compare_certifications(primary, isolated)["verdict"] == "PASS"
    isolated["phases"][0]["schemas"] = []
    assert compare_certifications(primary, isolated)["verdict"] == "REFUSE"


def test_clean_clone_audit_runs_representative_partition_and_cleans_up() -> None:
    result = audit_clean_checkout(
        ROOT,
        _evidence(),
        certified_range_head=RANGE_HEAD,
        checkout_head=_head(),
        representative_tests=["tests/test_phase4lk_runtime_config_snapshot.py"],
    )
    assert result["verdict"] == "PASS"
    assert result["inventory"]["verified_count"] == 75
    assert result["certification_comparison"]["semantic_match"] is True
    assert result["representative_tests"]["verdict"] == "PASS"
    assert result["cleanup_verified"] is True


def test_wrong_range_head_and_test_output_substitution_refuse() -> None:
    wrong = audit_clean_checkout(
        ROOT,
        _evidence(),
        certified_range_head="0" * 40,
        checkout_head=_head(),
        representative_tests=["tests/test_phase4lk_runtime_config_snapshot.py"],
    )
    assert wrong["verdict"] == "REFUSE"
    evidence = _evidence()
    evidence["result_summary"] = "forged passed"
    refused = certify_workstream(ROOT, evidence, expected_head=RANGE_HEAD)
    assert "TEST_EVIDENCE_HASH_INVALID" in refused["errors"]


def test_evidence_generation_and_audit_are_deterministic_and_inert() -> None:
    assert _evidence() == _evidence()
    result = audit_clean_checkout(
        ROOT,
        _evidence(),
        certified_range_head=RANGE_HEAD,
        checkout_head=_head(),
        representative_tests=["tests/test_phase4lk_runtime_config_snapshot.py"],
    )
    safety = result["safety"]
    assert safety["temporary_local_clone_only"] is True
    assert all(
        value is False for key, value in safety.items() if key != "temporary_local_clone_only"
    )
