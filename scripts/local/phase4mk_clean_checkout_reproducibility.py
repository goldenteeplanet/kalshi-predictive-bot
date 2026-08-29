"""Independent clean-clone reproducibility audit for Phase 4MJ certification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts.local.phase4mj_recovery_workstream_certification import (
    PHASE_META,
    certify_workstream,
    phase_paths,
)

SCHEMA = "phase4mk.clean-checkout-reproducibility-audit.v1"
DEFAULT_TESTS = [
    "tests/test_phase4lk_runtime_config_snapshot.py",
    "tests/test_phase4lz_disaster_recovery_simulation.py",
    "tests/test_phase4ma_recovery_state_machine.py",
    "tests/test_phase4mi_differential_mutation_audit.py",
    "tests/test_phase4mj_recovery_workstream_certification.py",
]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _run(
    args: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )


def inventory_from_certification(certification: object) -> list[dict[str, str]]:
    if not isinstance(certification, dict) or certification.get("verdict") != "PASS":
        return []
    return [
        {"path": file["path"], "content_sha256": file["content_sha256"]}
        for phase in certification.get("phases", [])
        for file in phase.get("files", [])
    ]


def verify_export_inventory(root: str | Path, expected: object) -> dict[str, object]:
    export = Path(root).resolve()
    errors: list[str] = []
    if not isinstance(expected, list) or any(
        not isinstance(row, dict) or set(row) != {"path", "content_sha256"} for row in expected
    ):
        errors.append("EXPECTED_INVENTORY_INVALID")
        expected = []
    expected_paths = {str(row["path"]): str(row["content_sha256"]) for row in expected}
    records: list[dict[str, str]] = []
    for relative, expected_hash in sorted(expected_paths.items()):
        path = (export / relative).resolve()
        try:
            path.relative_to(export)
        except ValueError:
            errors.append(f"PATH_ESCAPE:{relative}")
            continue
        if not path.is_file():
            errors.append(f"MISSING_BLOB:{relative}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected_hash:
            errors.append(f"BLOB_CORRUPTION_OR_SCHEMA_DRIFT:{relative}")
        records.append({"path": relative, "content_sha256": actual})
    owned_names = {Path(path).name for path in expected_paths}
    for directory in ("docs", "scripts/local", "tests"):
        base = export / directory
        if not base.is_dir():
            continue
        for path in base.glob("*phase4*.py" if directory != "docs" else "phase4*.md"):
            name = path.name
            phase_token = next(
                (phase.lower() for phase in PHASE_META if f"phase{phase.lower()}" in name), None
            )
            if phase_token and name not in owned_names:
                errors.append(f"UNTRACKED_SUBSTITUTION:{path.relative_to(export).as_posix()}")
    result: dict[str, object] = {
        "verdict": "PASS" if not errors and len(records) == len(expected_paths) else "REFUSE",
        "errors": sorted(set(errors)),
        "expected_count": len(expected_paths),
        "verified_count": len(records),
        "inventory_sha256": _digest(records),
    }
    result["verification_sha256"] = _digest(result)
    return result


def normalize_certification(certification: object) -> dict[str, object]:
    if not isinstance(certification, dict):
        return {"invalid": True}
    normalized = json.loads(json.dumps(certification))
    for field in ("repository", "repository_head_commit", "certification_sha256"):
        normalized.pop(field, None)
    return normalized


def compare_certifications(primary: object, isolated: object) -> dict[str, object]:
    primary_normalized = normalize_certification(primary)
    isolated_normalized = normalize_certification(isolated)
    result: dict[str, object] = {
        "verdict": "PASS" if primary_normalized == isolated_normalized else "REFUSE",
        "primary_semantics_sha256": _digest(primary_normalized),
        "isolated_semantics_sha256": _digest(isolated_normalized),
        "semantic_match": primary_normalized == isolated_normalized,
    }
    result["comparison_sha256"] = _digest(result)
    return result


def audit_clean_checkout(
    repo: str | Path,
    test_evidence: object,
    *,
    certified_range_head: str,
    checkout_head: str,
    representative_tests: list[str] | None = None,
    python_executable: str = sys.executable,
    temporary_parent: str | Path | None = None,
) -> dict[str, object]:
    source = Path(repo).resolve()
    source_before = _digest(test_evidence)
    errors: list[str] = []
    primary = certify_workstream(
        source,
        test_evidence,
        expected_head=certified_range_head,
    )
    if primary["verdict"] != "PASS":
        errors.append("PRIMARY_CERTIFICATION_REFUSED")
    expected_inventory = inventory_from_certification(primary)
    tests = representative_tests or DEFAULT_TESTS
    if not tests or any(
        test
        not in [path for phase in PHASE_META for path in phase_paths(phase)]
        + ["tests/test_phase4mj_recovery_workstream_certification.py"]
        for test in tests
    ):
        errors.append("REPRESENTATIVE_TEST_PARTITION_INVALID")
    parent = Path(temporary_parent).resolve() if temporary_parent else None
    if parent is not None and not parent.is_dir():
        errors.append("TEMPORARY_PARENT_INVALID")
    clone_path: Path | None = None
    cleanup_verified = False
    inventory: dict[str, object] = {"verdict": "REFUSE", "errors": ["NOT_RUN"]}
    isolated: dict[str, object] = {"verdict": "REFUSE", "errors": ["NOT_RUN"]}
    comparison: dict[str, object] = {"verdict": "REFUSE", "semantic_match": False}
    test_record: dict[str, object] = {"verdict": "REFUSE", "errors": ["NOT_RUN"]}
    if not errors:
        with tempfile.TemporaryDirectory(prefix="phase4mk-", dir=parent) as temporary:
            clone_path = Path(temporary).resolve() / "checkout"
            boundary = parent or Path(temporary).resolve().parent
            try:
                clone_path.relative_to(boundary)
            except ValueError:
                errors.append("TEMPORARY_BOUNDARY_INVALID")
            clone = _run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-hardlinks",
                    "--no-checkout",
                    str(source),
                    str(clone_path),
                ],
                cwd=source,
            )
            if clone.returncode != 0:
                errors.append("LOCAL_CLONE_FAILED")
            else:
                checkout = _run(
                    ["git", "checkout", "--quiet", "--detach", checkout_head], cwd=clone_path
                )
                if checkout.returncode != 0:
                    errors.append("CHECKOUT_HEAD_INVALID")
                else:
                    inventory = verify_export_inventory(clone_path, expected_inventory)
                    if inventory["verdict"] != "PASS":
                        errors.append("ISOLATED_INVENTORY_REFUSED")
                    isolated = certify_workstream(
                        clone_path,
                        test_evidence,
                        expected_head=certified_range_head,
                    )
                    if isolated["verdict"] != "PASS":
                        errors.append("ISOLATED_CERTIFICATION_REFUSED")
                    comparison = compare_certifications(primary, isolated)
                    if comparison["verdict"] != "PASS":
                        errors.append("CERTIFICATION_SEMANTICS_MISMATCH")
                    environment = os.environ.copy()
                    environment["PYTHONPATH"] = str(clone_path)
                    test_run = _run(
                        [
                            python_executable,
                            "-m",
                            "pytest",
                            "-q",
                            "-p",
                            "no:cacheprovider",
                            *tests,
                        ],
                        cwd=clone_path,
                        env=environment,
                    )
                    summary_match = re.search(r"(?m)(\d+) passed", test_run.stdout)
                    summary = f"{summary_match.group(1)} passed" if summary_match else ""
                    test_record = {
                        "verdict": "PASS" if test_run.returncode == 0 and summary else "REFUSE",
                        "test_paths": tests,
                        "command_sha256": _digest(tests),
                        "summary": summary,
                        "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(),
                    }
                    if test_record["verdict"] != "PASS":
                        errors.append("REPRESENTATIVE_TESTS_REFUSED")
        cleanup_verified = clone_path is not None and not clone_path.parent.exists()
        if not cleanup_verified:
            errors.append("TEMPORARY_CLEANUP_UNVERIFIED")
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "certified_range_head": certified_range_head,
        "checkout_head": checkout_head,
        "primary_semantics_sha256": comparison.get("primary_semantics_sha256"),
        "isolated_semantics_sha256": comparison.get("isolated_semantics_sha256"),
        "inventory": inventory,
        "certification_comparison": comparison,
        "representative_tests": test_record,
        "cleanup_verified": cleanup_verified,
        "test_evidence_unchanged": _digest(test_evidence) == source_before,
        "safety": {
            "temporary_local_clone_only": True,
            "primary_checkout_write": False,
            "network_access": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["audit_sha256"] = _digest(result)
    return result
