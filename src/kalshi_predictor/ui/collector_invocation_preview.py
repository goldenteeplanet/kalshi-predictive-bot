from __future__ import annotations

import hashlib
from pathlib import Path


REQUIRED_EXEC_TOKENS = (
    "--service kalshi-r5-bounded.service",
    "--timer kalshi-r5-bounded.timer",
    "--legacy-service kalshi-r5-watcher.service",
    "--roadmap /opt/kalshi-predictive-bot/reports/phase_ui_obs5f/inputs/objective_20_phase_status_20260719.json",
    "--r5-certification /opt/kalshi-predictive-bot/reports/phase_ui_obs5f/inputs/r5_recovery9_deployment_certification.json",
)


def certify_invocation_preview(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    exec_lines = [line for line in lines if line.startswith("ExecStart=")]
    failures: list[str] = []
    if len(exec_lines) != 1:
        failures.append("EXECSTART_COUNT_INVALID")
        invocation = ""
    else:
        invocation = exec_lines[0]
    for token in REQUIRED_EXEC_TOKENS:
        if token not in invocation:
            failures.append("REQUIRED_ARGUMENT_MISSING:" + token.split()[0])
    required_guards = (
        "Environment=UI_READ_ONLY=true", "Environment=EXECUTION_ENABLED=false",
        "Environment=EXECUTION_DRY_RUN=true", "Environment=EXECUTION_KILL_SWITCH=true",
        "TimeoutStartSec=30", "NoNewPrivileges=true", "ProtectSystem=strict",
        "MemoryMax=192M", "TasksMax=32",
        "ReadWritePaths=/opt/kalshi-predictive-bot/reports/ui_obs_live",
    )
    for guard in required_guards:
        if guard not in lines:
            failures.append("SAFETY_GUARD_MISSING:" + guard.split("=", 1)[0])
    forbidden = ("systemctl start", "systemctl stop", "systemctl restart", "EXECUTION_ENABLED=true")
    for token in forbidden:
        if token in text:
            failures.append("FORBIDDEN_CONTROL_PRESENT:" + token)
    return {
        "phase": "UI-OBS-5F-A",
        "mode": "LOCAL_EXACT_INVOCATION_REPAIR_PREVIEW",
        "status": "PASSED" if not failures else "FAILED",
        "preview_path": str(path),
        "preview_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "failures": sorted(set(failures)),
        "exact_mapping": {
            "scheduler_service": "kalshi-r5-bounded.service",
            "scheduler_timer": "kalshi-r5-bounded.timer",
            "legacy_watcher": "kalshi-r5-watcher.service",
            "roadmap_phases": 20,
            "r5_recovery9_evidence": "explicit immutable input",
        },
        "guardrails": {
            "read_only": True, "database_writes": 0, "service_controls": 0,
            "execution_enabled": False, "bounded_timeout_seconds": 30,
            "deployment_performed": False,
        },
        "retry_requires_explicit_approval": True,
    }
