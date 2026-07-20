"""Atomic offline PROV-14B R2B-to-R2A certification pipeline."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase_prov14b_r2a import build_certification_bundle
from kalshi_predictor.phase_prov14b_r2b import capture_runtime_evidence


def run_capture_certification_pipeline(
    *,
    capture_kwargs: dict[str, Any],
    rollback_root: Path,
    as_of: datetime,
    synthetic_preview: bool = False,
) -> dict[str, Any]:
    """Capture, re-hash, certify, and re-hash without intermediate publication."""
    capture = capture_runtime_evidence(**capture_kwargs)
    unchanged_before = _sources_unchanged(capture.get("sources"))
    certification = None
    if capture.get("status") == "PASSED" and unchanged_before:
        certification = build_certification_bundle(
            **capture["r2a_inputs"],
            rollback_root=rollback_root,
            as_of=as_of,
            synthetic_preview=synthetic_preview,
        )
    unchanged_after = _sources_unchanged(capture.get("sources"))
    gates = {
        "capture_passed": capture.get("status") == "PASSED",
        "sources_unchanged_before_certification": unchanged_before,
        "certification_executed": certification is not None,
        "certification_passed": certification is not None
        and certification.get("status") == "PASSED",
        "sources_unchanged_after_certification": unchanged_after,
    }
    passed = all(gates.values())
    report: dict[str, Any] = {
        "phase": "PROV-14B-R2C",
        "mode": (
            "LOCAL_SYNTHETIC_ATOMIC_CAPTURE_CERTIFICATION_CI_PREVIEW"
            if synthetic_preview
            else "LOCAL_OFFLINE_ATOMIC_CAPTURE_CERTIFICATION_CI"
        ),
        "status": "PASSED" if passed else "FAILED",
        "gates": gates,
        "failed_gates": sorted(key for key, value in gates.items() if not value),
        "capture": capture,
        "certification": certification,
        "summary": {
            "pipeline_passed": passed,
            "runtime_certified": bool(
                passed
                and certification
                and certification.get("summary", {}).get("runtime_certified") is True
            ),
            "deployment_or_execution_authorized": False,
            "ci_exit_code": 0 if passed else 2,
        },
        "guardrails": {
            "cloud_access": False,
            "database_opened": False,
            "database_writes": 0,
            "service_changes": 0,
            "threshold_changes": 0,
            "execution_changes": 0,
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical(report).encode()).hexdigest()
    return report


def write_pipeline_report(report: dict[str, Any], output: Path) -> Path:
    """Publish exactly one combined report with atomic replacement."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(_canonical(report), encoding="utf-8")
    temporary.replace(output)
    return output


def _sources_unchanged(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for row in value:
        if not isinstance(row, dict):
            return False
        path = Path(str(row.get("path") or ""))
        if not path.is_file() or path.stat().st_size != row.get("size_bytes"):
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != row.get("sha256"):
            return False
    return True


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
