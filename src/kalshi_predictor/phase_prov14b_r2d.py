"""PROV-14B-R2D repository-local CI workflow safety preview."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

WORKFLOW = Path(".github/workflows/prov14b-r2c-offline-certification.yml")
BANNED_SURFACES = (
    "secrets.",
    "continue-on-error",
    "ssh ",
    "scp ",
    "systemctl",
    "/var/lib/kalshi-bot",
    "sqlite3 ",
    "EXECUTION_ENABLED=true",
)


def build_ci_workflow_preview(project_root: Path) -> dict[str, Any]:
    path = project_root / WORKFLOW
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    checks = {
        "workflow_present": bool(text),
        "pull_request_and_manual_only": "pull_request:" in text
        and "workflow_dispatch:" in text
        and "schedule:" not in text,
        "read_only_repository_permission": "permissions:\n  contents: read" in text,
        "bounded_timeout": "timeout-minutes: 10" in text,
        "runs_full_r2_regression_matrix": all(
            name in text
            for name in (
                "tests/test_phase_prov14b_r2a.py",
                "tests/test_phase_prov14b_r2b.py",
                "tests/test_phase_prov14b_r2c.py",
            )
        ),
        "runs_atomic_r2c_gate": "python scripts/prov14b_r2c_preview.py" in text,
        "retains_report_even_on_failure": "if: always()" in text
        and "actions/upload-artifact@v4" in text,
        "artifact_missing_is_error": "if-no-files-found: error" in text,
        "retention_bounded_30_days": "retention-days: 30" in text,
        "no_privileged_or_runtime_surfaces": not any(token in text for token in BANNED_SURFACES),
    }
    report: dict[str, Any] = {
        "phase": "PROV-14B-R2D",
        "mode": "LOCAL_REPOSITORY_CI_AND_ARTIFACT_RETENTION_PREVIEW",
        "status": "PASSED" if all(checks.values()) else "FAILED",
        "workflow_path": str(WORKFLOW),
        "workflow_sha256": hashlib.sha256(text.encode()).hexdigest() if text else None,
        "checks": checks,
        "failed_checks": sorted(key for key, value in checks.items() if not value),
        "artifact": {
            "path": "reports/phase_prov14b_r2c/prov14b_r2c_ci_preview.json",
            "retention_days": 30,
            "retained_on_failure": True,
        },
        "guardrails": {
            "cloud_access": False,
            "database_access": False,
            "database_writes": 0,
            "deployment_connected": False,
            "runtime_controls": False,
            "execution_activation": False,
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical(report).encode()).hexdigest()
    return report


def write_ci_workflow_preview(project_root: Path, output_dir: Path) -> Path:
    report = build_ci_workflow_preview(project_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov14b_r2d_ci_workflow_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(_canonical(report), encoding="utf-8")
    temporary.replace(path)
    return path


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
