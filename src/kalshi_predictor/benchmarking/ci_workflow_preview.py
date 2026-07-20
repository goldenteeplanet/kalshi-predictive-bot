from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


WORKFLOW = Path(".github/workflows/pmb28-offline-certification.yml")


def build_ci_workflow_integration_preview(project_root: Path) -> dict[str, Any]:
    path = project_root / WORKFLOW
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    checks = {
        "manual_or_pull_request_only": "pull_request:" in text and "workflow_dispatch:" in text,
        "read_only_permissions": "permissions:\n  contents: read" in text,
        "runs_pmb28_gate": "scripts/pmb28_certification_ci.py" in text,
        "artifact_uploaded_on_failure": "if: always()" in text,
        "pmb28_report_preserved": (
            "reports/phase_pmb28/pmb28_offline_certification_ci_gate.json" in text
        ),
        "no_deployment_step": all(
            token not in text.lower()
            for token in ("deploy", "ssh ", "scp ", "kubectl", "terraform", "docker push")
        ),
        "no_secret_reference": "secrets." not in text.lower(),
        "no_runtime_activation": all(
            token not in text for token in (
                "EXECUTION_ENABLED=true", "LIVE_EXECUTION_ENABLED=true", "policy_activated=true"
            )
        ),
    }
    return {
        "phase": "PMB-29",
        "mode": "LOCAL_CI_WORKFLOW_INTEGRATION_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "runtime_deployment_connected": False,
        "policy_activation_connected": False,
        "workflow": {
            "path": WORKFLOW.as_posix(),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "checks": checks,
        "summary": {
            "passed": all(checks.values()),
            "checks_passed": sum(checks.values()),
            "checks_total": len(checks),
        },
    }


def write_ci_workflow_integration_preview(project_root: Path, output_dir: Path) -> Path:
    report = build_ci_workflow_integration_preview(project_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb29_local_ci_workflow_integration_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
