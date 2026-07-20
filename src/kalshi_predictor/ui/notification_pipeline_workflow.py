from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


WORKFLOW = Path(".github/workflows/ui-obs2h-notification-pipeline.yml")


def build_workflow_preview(project_root: Path) -> dict[str, Any]:
    path = project_root / WORKFLOW
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lowered = text.lower()
    checks = {
        "workflow_present": bool(text),
        "runs_ui_obs2h_gate": "scripts/ui_obs2h_notification_pipeline_ci.py" in text,
        "golden_manifest_supplied": "tests/golden/ui_obs2h_notification_pipeline_golden.json" in text,
        "fails_on_gate_exit_code": "continue-on-error" not in lowered,
        "artifact_retained_always": "if: always()" in text and "actions/upload-artifact@v4" in text,
        "missing_artifact_fails": "if-no-files-found: error" in text,
        "bounded_retention": "retention-days: 30" in text,
        "bounded_runtime": "timeout-minutes: 10" in text,
        "read_only_permissions": "permissions:\n  contents: read" in text,
        "no_deployment": not any(token in lowered for token in ("deploy", "ssh", "tailscale")),
        "no_real_notifications": not any(token in lowered for token in ("send-mail", "slack", "webhook", "notify-send")),
        "no_secrets": "secrets." not in lowered,
    }
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
    return {
        "phase": "UI-OBS-2I",
        "mode": "LOCAL_CI_WORKFLOW_AND_ARTIFACT_RETENTION_PREVIEW",
        "status": "PASSED" if all(checks.values()) else "FAILED",
        "workflow": WORKFLOW.as_posix(),
        "workflow_sha256": digest,
        "checks": checks,
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "deployment_connected": False,
        "actual_notifications_sent": 0,
        "execution_changed": False,
    }


def write_workflow_preview(project_root: Path, output_dir: Path) -> Path:
    report = build_workflow_preview(project_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "ui_obs2i_local_ci_workflow_preview.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
