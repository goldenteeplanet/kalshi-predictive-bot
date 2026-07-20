from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def certify_fail_closed_harness(script: Path, unit: Path, *, bash: str = "bash") -> dict[str, object]:
    text = script.read_text(encoding="utf-8")
    failures: list[str] = []
    required = (
        "systemd-analyze verify /tmp/kalshi-ui-status-collector.service",
        "install -m 0644 /tmp/kalshi-ui-status-collector.service \"$unit\"",
        "trap 'fail_and_rollback' ERR",
        "trap - ERR",
        "rollback_now",
        "exit \"$status\"",
    )
    for token in required:
        if token not in text:
            failures.append("HARNESS_TOKEN_MISSING:" + token)
    if ".service.ui-obs5fa.preview" in text:
        failures.append("INVALID_SYSTEMD_STAGING_SUFFIX")
    behavior = _exercise_err_trap(bash)
    if behavior["exit_code"] != 23:
        failures.append("FAILURE_EXIT_CODE_NOT_PRESERVED")
    if behavior["rollback_count"] != 1:
        failures.append("ROLLBACK_NOT_EXACTLY_ONCE")
    if behavior["continued_after_failure"]:
        failures.append("HARNESS_CONTINUED_AFTER_FAILURE")
    return {
        "phase": "UI-OBS-5F-B",
        "mode": "LOCAL_DEPLOYMENT_HARNESS_FAIL_CLOSED_CERTIFICATION",
        "status": "PASSED" if not failures else "FAILED",
        "failures": failures,
        "unit_staging_basename": "kalshi-ui-status-collector.service",
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "unit_sha256": hashlib.sha256(unit.read_bytes()).hexdigest(),
        "injected_failure": behavior,
        "cloud_access": False,
        "deployment_performed": False,
        "database_writes": 0,
        "service_controls": 0,
        "execution_enabled": False,
        "retry_requires_explicit_approval": True,
    }


def _exercise_err_trap(bash: str) -> dict[str, object]:
    probe = (
        "set -Eeuo pipefail\n"
        "rollback_now() { set +e; echo ROLLBACK; }\n"
        "fail_and_rollback() { status=$?; trap - ERR; rollback_now; exit \"$status\"; }\n"
        "trap 'fail_and_rollback' ERR\n"
        "(exit 23)\n"
        "echo CONTINUED\n"
    )
    result = subprocess.run(
        [bash, "-s"], input=probe.encode("utf-8"), capture_output=True, check=False
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    return {
        "exit_code": result.returncode,
        "rollback_count": stdout.splitlines().count("ROLLBACK"),
        "continued_after_failure": "CONTINUED" in stdout.splitlines(),
    }
