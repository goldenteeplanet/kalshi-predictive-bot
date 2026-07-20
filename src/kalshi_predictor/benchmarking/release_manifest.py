from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


REPORTS = tuple(
    Path(f"reports/phase_pmb{number}") / filename
    for number, filename in (
        (22, "pmb22_interaction_boundary_refinement.json"),
        (23, "pmb23_stress_aware_allocation_guard_preview.json"),
        (24, "pmb24_drawdown_aware_guard_refinement_preview.json"),
        (25, "pmb25_oos_exposure_guard_validation.json"),
        (26, "pmb26_multi_seed_oos_exposure_stability_census.json"),
        (27, "pmb27_exposure_guard_golden_certification_bundle.json"),
        (28, "pmb28_offline_certification_ci_gate.json"),
        (29, "pmb29_local_ci_workflow_integration_preview.json"),
        (30, "pmb30_offline_ci_failure_mode_matrix.json"),
        (31, "pmb31_cross_environment_reproducibility_preview.json"),
    )
)
GOLDENS = (
    Path("tests/golden/pmb22_interaction_boundary_summary.json"),
    Path("tests/golden/pmb27_exposure_guard_bundle_summary.json"),
)
WORKFLOW = Path(".github/workflows/pmb28-offline-certification.yml")
COMMANDS = {
    f"PMB-{number}": f"python scripts/pmb{number}_{suffix}.py"
    for number, suffix in (
        (22, "interaction_boundary"),
        (23, "stress_guard"),
        (24, "drawdown_guard"),
        (25, "oos_exposure_guard"),
        (26, "exposure_stability"),
        (27, "exposure_bundle"),
        (28, "certification_ci"),
        (29, "ci_workflow_preview"),
        (30, "ci_failure_matrix"),
        (31, "environment_reproducibility"),
    )
}


def build_benchmark_release_manifest(project_root: Path) -> dict[str, Any]:
    paths = REPORTS + GOLDENS + (WORKFLOW, Path("pyproject.toml"))
    files = [_file_entry(project_root, path) for path in paths]
    phase_reports = [json.loads((project_root / path).read_text()) for path in REPORTS]
    compatibility = {
        "python_requires": ">=3.11",
        "ci_python_matrix": ["3.11", "3.12", "3.13"],
        "locally_certified_python": ["3.12"],
        "operating_system_ci": ["ubuntu-latest"],
        "cross_version_execution_status": "PENDING_CI",
    }
    canonical = json.dumps(
        {"files": files, "commands": COMMANDS, "compatibility": compatibility},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return {
        "phase": "PMB-32",
        "mode": "LOCAL_DETERMINISTIC_BENCHMARK_RELEASE_MANIFEST",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "policy_activated": False,
        "release_label": "pmb-exposure-guard-preview-v1",
        "files": files,
        "commands": COMMANDS,
        "compatibility": compatibility,
        "certification": {
            "all_source_phases_execution_disabled": all(
                report["execution_enabled"] is False for report in phase_reports
            ),
            "all_source_phases_database_write_free": all(
                report["database_writes"] == 0 for report in phase_reports
            ),
            "pmb27_certification_passed": phase_reports[5]["certification"]["passed"] is True,
            "pmb28_ci_gate_passed": phase_reports[6]["summary"]["passed"] is True,
            "runtime_activation_authorized": False,
        },
        "summary": {
            "file_count": len(files),
            "report_count": len(REPORTS),
            "golden_count": len(GOLDENS),
            "command_count": len(COMMANDS),
            "manifest_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_benchmark_release_manifest(project_root: Path, output_dir: Path) -> Path:
    manifest = build_benchmark_release_manifest(project_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb32_benchmark_release_manifest.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    checksums = output_dir / "pmb32_SHA256SUMS.txt"
    checksum_text = "".join(
        f"{row['sha256']}  {row['path']}\n" for row in manifest["files"]
    )
    checksum_tmp = checksums.with_suffix(".txt.tmp")
    checksum_tmp.write_text(checksum_text, encoding="utf-8")
    checksum_tmp.replace(checksums)
    return path


def _file_entry(project_root: Path, relative: Path) -> dict[str, Any]:
    raw = (project_root / relative).read_bytes()
    return {
        "path": relative.as_posix(),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
