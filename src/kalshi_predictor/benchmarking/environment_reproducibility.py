from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SUPPORTED_PYTHON_VERSIONS = ("3.11", "3.12", "3.13")
HASH_SEEDS = ("0", "1", "8675309")


def build_cross_environment_reproducibility_preview(
    project_root: Path, workspace: Path
) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    runs = []
    for hash_seed in HASH_SEEDS:
        output_dir = workspace / f"hash_seed_{hash_seed}"
        command = [
            sys.executable,
            str(project_root / "scripts/pmb28_certification_ci.py"),
            "--project-root", str(project_root),
            "--output-dir", str(output_dir),
        ]
        completed = subprocess.run(
            command,
            cwd=project_root,
            env=_clean_environment(hash_seed),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        report_path = output_dir / "pmb28_offline_certification_ci_gate.json"
        report = json.loads(report_path.read_text()) if report_path.is_file() else {}
        runs.append({
            "python_hash_seed": hash_seed,
            "exit_code": completed.returncode,
            "passed": report.get("summary", {}).get("passed") is True,
            "bundle_digest": report.get("summary", {}).get("regenerated_bundle_digest"),
            "summary_digest": report.get("summary", {}).get("actual_summary_digest"),
            "stderr_empty": not completed.stderr.strip(),
        })
    workflow = (project_root / ".github/workflows/pmb28-offline-certification.yml").read_text()
    matrix_declared = all(f'"{version}"' in workflow for version in SUPPORTED_PYTHON_VERSIONS)
    bundle_digests = {row["bundle_digest"] for row in runs}
    summary_digests = {row["summary_digest"] for row in runs}
    current_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    canonical = json.dumps(runs, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-31",
        "mode": "LOCAL_CROSS_ENVIRONMENT_REPRODUCIBILITY_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "supported_python_versions": list(SUPPORTED_PYTHON_VERSIONS),
        "locally_executed_python_versions": [current_version],
        "ci_matrix_declared": matrix_declared,
        "cross_version_execution_pending_ci": set(SUPPORTED_PYTHON_VERSIONS) != {current_version},
        "clean_environment_runs": runs,
        "summary": {
            "local_run_count": len(runs),
            "all_local_runs_passed": all(row["exit_code"] == 0 and row["passed"] for row in runs),
            "bundle_digest_reproducible": len(bundle_digests) == 1,
            "summary_digest_reproducible": len(summary_digests) == 1,
            "ci_matrix_covers_supported_versions": matrix_declared,
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_cross_environment_reproducibility_preview(
    project_root: Path, output_dir: Path
) -> Path:
    report = build_cross_environment_reproducibility_preview(
        project_root, output_dir / "clean_runs"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb31_cross_environment_reproducibility_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _clean_environment(hash_seed: str) -> dict[str, str]:
    allowed = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "HOME", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "VIRTUAL_ENV"}
    }
    allowed.update({
        "PYTHONHASHSEED": hash_seed,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "EXECUTION_ENABLED": "false",
    })
    return allowed
