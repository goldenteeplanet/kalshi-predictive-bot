from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class PreflightResult:
    payload: dict[str, Any]

    @property
    def passed(self) -> bool:
        return self.payload["status"] == "PASSED"


def certify_cloud_preflight(observed: Mapping[str, Any]) -> PreflightResult:
    """Certify a captured, read-only cloud inspection; never contacts the host."""
    required = {
        "execution_enabled": False,
        "ui_read_only": True,
        "service_active": True,
        "loopback_only": True,
        "root_health_http": 200,
        "writer_clear": True,
        "db_locks_clear": True,
        "new_oom": False,
    }
    checks = [
        {
            "name": name,
            "expected": expected,
            "observed": observed.get(name),
            "passed": observed.get(name) == expected,
        }
        for name, expected in required.items()
    ]
    blockers = [row["name"] for row in checks if not row["passed"]]
    warnings: list[str] = []
    if observed.get("project_world_writable"):
        warnings.append("PROJECT_WORLD_WRITABLE")
    if observed.get("reports_world_writable"):
        warnings.append("REPORTS_WORLD_WRITABLE")
    if observed.get("memory_max") in {None, "", "infinity"}:
        warnings.append("SERVICE_MEMORY_LIMIT_UNBOUNDED")
    if int(observed.get("root_available_gib") or 0) < 10:
        warnings.append("ROOT_FREE_SPACE_BELOW_10_GIB")
    if int(observed.get("backup_available_gib") or 0) < 10:
        warnings.append("BACKUP_FREE_SPACE_BELOW_10_GIB")

    canonical = json.dumps(dict(observed), sort_keys=True, separators=(",", ":")).encode()
    return PreflightResult(
        {
            "schema_version": "ui-obs-3a/v1",
            "phase": "UI-OBS-3A",
            "mode": "READ_ONLY_PREFLIGHT",
            "status": "PASSED" if not blockers else "BLOCKED",
            "deployment_performed": False,
            "cloud_mutation_performed": False,
            "checks": checks,
            "blockers": blockers,
            "warnings": warnings,
            "capture_sha256": hashlib.sha256(canonical).hexdigest(),
            "observed": dict(observed),
            "next_phase": "UI-OBS-3B",
            "next_phase_requires_explicit_approval": True,
        }
    )


def write_preflight_report(observed: Mapping[str, Any], output: Path) -> Path:
    result = certify_cloud_preflight(observed)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output
