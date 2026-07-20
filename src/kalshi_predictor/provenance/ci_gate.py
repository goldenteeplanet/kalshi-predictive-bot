from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kalshi_predictor.provenance.bundle import verify_offline_certification_bundle


def build_offline_ci_gate(
    *, bundle_path: Path, manifest_path: Path, root: Path
) -> dict[str, Any]:
    bundle = _json_object(bundle_path)
    verification = verify_offline_certification_bundle(bundle_path, root=root)
    expected_manifest = [
        f"{row['sha256']}  {row['path']}" for row in bundle.get("artifacts", [])
    ]
    actual_manifest = manifest_path.read_text(encoding="utf-8").splitlines()
    checks = [
        _check("BUNDLE_PHASE_VALID", bundle.get("phase") == "PROV-15F"),
        _check("ARTIFACT_HASHES_VALID", verification["verified"], verification["failures"]),
        _check("MANIFEST_MATCHES_BUNDLE", actual_manifest == expected_manifest),
        _check(
            "TOOLING_BUNDLE_VALID",
            bundle.get("summary", {}).get("tooling_bundle_valid") is True,
        ),
        _check("EXECUTION_DISABLED", bundle.get("execution_enabled") is False),
        _check("DATABASE_ACCESS_DISABLED", bundle.get("database_access") is False),
    ]
    passed = all(check["passed"] for check in checks)
    return {
        "phase": "PROV-15G",
        "generated_at": bundle.get("generated_at"),
        "mode": "LOCAL_CI_ARTIFACT_DRIFT_GATE",
        "passed": passed,
        "exit_code": 0 if passed else 1,
        "database_access": False,
        "cloud_runtime_access": False,
        "execution_enabled": False,
        "runtime_attribution_release_ready": bundle.get("summary", {}).get(
            "runtime_attribution_release_ready", False
        ),
        "summary": {
            "checks": len(checks),
            "checks_passed": sum(check["passed"] for check in checks),
            "checks_failed": sum(not check["passed"] for check in checks),
            "artifact_drift_detected": not verification["verified"],
            "manifest_drift_detected": actual_manifest != expected_manifest,
        },
        "checks": checks,
        "guardrails": {
            "source_artifacts_modified": False,
            "manifest_modified": False,
            "database_writes": 0,
            "execution_enabled": False,
        },
    }


def write_offline_ci_gate(
    *, bundle_path: Path, manifest_path: Path, root: Path, output_path: Path
) -> tuple[Path, int]:
    report = build_offline_ci_gate(
        bundle_path=bundle_path, manifest_path=manifest_path, root=root
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return output_path, int(report["exit_code"])


def _json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("bundle must be a JSON object")
    return payload


def _check(name: str, passed: bool, details: Any = None) -> dict[str, Any]:
    row = {"check": name, "passed": bool(passed)}
    if details:
        row["details"] = details
    return row
