from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

REQUIRED_PHASES = ("PROV-15", "PROV-15B", "PROV-15C", "PROV-15D", "PROV-15E")


def build_offline_certification_bundle(
    artifacts: Mapping[str, Path], *, generated_at: datetime, root: Path
) -> dict[str, Any]:
    missing = [phase for phase in REQUIRED_PHASES if phase not in artifacts]
    extra = sorted(set(artifacts) - set(REQUIRED_PHASES))
    if missing or extra:
        raise ValueError(f"artifact phase mismatch: missing={missing}, extra={extra}")
    entries = []
    payloads = {}
    for phase in REQUIRED_PHASES:
        logical_path = artifacts[phase].absolute()
        path = logical_path.resolve()
        if not path.is_file():
            raise ValueError(f"artifact missing for {phase}: {path}")
        data = path.read_bytes()
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError(f"artifact for {phase} must be a JSON object")
        if payload.get("phase") != phase:
            raise ValueError(f"artifact phase mismatch for {phase}: {payload.get('phase')}")
        payloads[phase] = payload
        entries.append({
            "phase": phase,
            "path": _relative_path(logical_path, root.absolute()),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    checks = _cross_report_checks(payloads)
    bundle_valid = all(check["passed"] for check in checks)
    runtime_ready = bool(
        payloads["PROV-15B"].get("summary", {}).get("passed")
        and payloads["PROV-15E"].get("certification", {}).get("after_passed")
    )
    return {
        "phase": "PROV-15F",
        "generated_at": generated_at.isoformat(),
        "mode": "OFFLINE_CERTIFICATION_REPRODUCIBILITY_BUNDLE",
        "database_access": False,
        "cloud_runtime_access": False,
        "execution_enabled": False,
        "summary": {
            "artifact_count": len(entries),
            "cross_report_checks": len(checks),
            "cross_report_checks_passed": sum(check["passed"] for check in checks),
            "tooling_bundle_valid": bundle_valid,
            "runtime_attribution_release_ready": runtime_ready,
        },
        "artifacts": entries,
        "cross_report_consistency": checks,
        "guardrails": {
            "source_artifacts_modified": False,
            "thresholds_changed": False,
            "database_writes": 0,
            "execution_enabled": False,
        },
    }


def write_offline_certification_bundle(
    artifacts: Mapping[str, Path],
    *,
    generated_at: datetime,
    root: Path,
    output_dir: Path,
) -> tuple[Path, Path]:
    bundle = build_offline_certification_bundle(
        artifacts, generated_at=generated_at, root=root
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = output_dir / "prov15f_offline_certification_bundle.json"
    _atomic_write(bundle_path, json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    manifest_path = output_dir / "MANIFEST.sha256"
    lines = [f"{row['sha256']}  {row['path']}" for row in bundle["artifacts"]]
    _atomic_write(manifest_path, "\n".join(lines) + "\n")
    return bundle_path, manifest_path


def verify_offline_certification_bundle(bundle_path: Path, *, root: Path) -> dict[str, Any]:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    failures = []
    for row in bundle.get("artifacts", []):
        path = root / row["path"]
        if not path.is_file():
            failures.append({"phase": row["phase"], "failure": "ARTIFACT_MISSING"})
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != row["sha256"]:
            failures.append({"phase": row["phase"], "failure": "SHA256_MISMATCH"})
    return {
        "verified": not failures,
        "artifacts_checked": len(bundle.get("artifacts", [])),
        "failures": failures,
    }


def _cross_report_checks(payloads: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    prov15b = payloads["PROV-15B"]
    prov15d = payloads["PROV-15D"]
    prov15e = payloads["PROV-15E"]
    checks = [
        _check(
            "PROV15B_FAILURES_MATCH_PROV15D_TRIAGE",
            prov15b.get("summary", {}).get("events_failed")
            == prov15d.get("summary", {}).get("failed_rows"),
        ),
        _check(
            "PROV15E_BEFORE_FAILS_AFTER_PASSES",
            prov15e.get("certification", {}).get("before_passed") is False
            and prov15e.get("certification", {}).get("after_passed") is True,
        ),
        _check(
            "ALL_PHASES_EXECUTION_DISABLED",
            all(payload.get("execution_enabled") is False for payload in payloads.values()),
        ),
        _check(
            "ALL_PHASES_OFFLINE",
            all(
                payload.get("database_access") is False
                or payload.get("database_writes_by_analyzer") == 0
                for payload in payloads.values()
            ),
        ),
        _check(
            "PROV15C_COMPATIBILITY_FIXTURES_PRESENT",
            payloads["PROV-15C"].get("summary", {}).get("compatible", 0) >= 3,
        ),
    ]
    return checks


def _check(name: str, passed: bool) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed)}


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"artifact is outside bundle root: {path}") from exc


def _atomic_write(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)
