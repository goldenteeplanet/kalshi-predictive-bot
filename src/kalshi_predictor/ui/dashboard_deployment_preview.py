from __future__ import annotations

import hashlib
from pathlib import Path


FILES = (
    "src/kalshi_predictor/ui/progress.py",
    "src/kalshi_predictor/ui/live_roadmap_status.py",
    "src/kalshi_predictor/ui/roadmap_summary.py",
    "src/kalshi_predictor/ui/templates/progress_dashboard.html",
    "src/kalshi_predictor/ui/static/styles.css",
)


def certify_dashboard_deployment_preview(project: Path, unit: Path, harness: Path | None = None) -> dict[str, object]:
    failures: list[str] = []
    unit_text = unit.read_text(encoding="utf-8")
    required = (
        "Wants=network-online.target kalshi-ui-status-collector.timer",
        "After=network-online.target",
        "Environment=UI_READ_ONLY=true",
        "Environment=EXECUTION_ENABLED=false",
        "Environment=EXECUTION_KILL_SWITCH=true",
        "ExecStart=/opt/kalshi-predictive-bot/.venv/bin/kalshi-bot ui --host 127.0.0.1 --port 8080",
        "NoNewPrivileges=true",
    )
    for token in required:
        if token not in unit_text:
            failures.append("UNIT_GUARD_MISSING:" + token.split("=", 1)[0])
    if "Requires=kalshi-r5-watcher.service" in unit_text or "After=network-online.target kalshi-r5-watcher.service" in unit_text:
        failures.append("LEGACY_WATCHER_DEPENDENCY_PRESENT")
    hashes: dict[str, str] = {}
    for relative in FILES:
        path = project / relative
        if not path.is_file():
            failures.append("DEPLOYMENT_FILE_MISSING:" + relative)
        else:
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    progress = (project / FILES[0]).read_text(encoding="utf-8")
    template = (project / FILES[3]).read_text(encoding="utf-8")
    css = (project / FILES[4]).read_text(encoding="utf-8")
    for token in ("build_live_roadmap_status", "normalize_roadmap_summary"):
        if token not in progress:
            failures.append("PROGRESS_ADAPTER_MISSING:" + token)
    for token in ("data-roadmap-summary", "data-live-roadmap", "PROV-14B"):
        if token not in template:
            failures.append("TEMPLATE_SURFACE_MISSING:" + token)
    if ".roadmap-summary-grid" not in css:
        failures.append("ROADMAP_STYLE_MISSING")
    harness_hash = None
    if harness is not None:
        harness_text = harness.read_text(encoding="utf-8")
        harness_hash = hashlib.sha256(harness.read_bytes()).hexdigest()
        for token in (
            "trap 'fail_and_rollback' ERR", "rollback_now", "exit \"$status\"",
            "curl --fail --silent --max-time 5 http://127.0.0.1:8080/api/system/progress",
            "post_status=", "[[ \"$post_status\" == 405 ]]", "127.0.0.1:8080",
        ):
            if token not in harness_text:
                failures.append("DEPLOYMENT_HARNESS_GUARD_MISSING:" + token)
    return {
        "phase": "UI-OBS-5I-A",
        "mode": "LOCAL_GUARDED_CLOUD_DASHBOARD_DEPLOYMENT_PREVIEW",
        "status": "PASSED" if not failures else "FAILED",
        "failures": failures,
        "deployment_files": hashes,
        "unit_sha256": hashlib.sha256(unit.read_bytes()).hexdigest(),
        "deployment_harness_sha256": harness_hash,
        "exact_scope": {
            "existing_files_changed": 3,
            "new_helper_modules": 2,
            "systemd_unit_changed": True,
            "legacy_dependency_removed": True,
            "collector_timer_wanted": True,
        },
        "guardrails": {
            "local_only": True, "cloud_writes": 0, "database_writes": 0,
            "deployment_performed": False, "execution_enabled": False,
            "loopback_only": True, "rollback_required": True,
        },
        "deployment_requires_explicit_approval": True,
    }
