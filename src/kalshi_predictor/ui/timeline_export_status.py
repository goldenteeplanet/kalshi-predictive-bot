"""Read-only UI status for the UI-OBS-5P certification bundle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

EXPORT_NAMES = {
    "json": "certification_timeline.json",
    "csv": "certification_timeline.csv",
}


def build_timeline_export_status(reports_root: Path) -> dict[str, Any]:
    phase_root = reports_root / "phase_ui_obs5p"
    manifest_path = phase_root / "ui_obs5p_certification_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return _missing()
    failures = list(manifest.get("failures") or [])
    exports = []
    for kind, expected_name in EXPORT_NAMES.items():
        metadata = (manifest.get("exports") or {}).get(kind) or {}
        path = phase_root / expected_name
        expected_sha = str(metadata.get("sha256") or "")
        try:
            actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            actual_sha = None
            failures.append(f"{kind.upper()}_EXPORT_MISSING")
        verified = actual_sha is not None and actual_sha == expected_sha and len(expected_sha) == 64
        if actual_sha is not None and not verified:
            failures.append(f"{kind.upper()}_EXPORT_HASH_MISMATCH")
        exports.append({
            "kind": kind.upper(),
            "name": expected_name,
            "sha256": expected_sha or None,
            "verified": verified,
            "href": f"/system/progress/certification-export/{kind}" if verified else None,
        })
    bundle_sha = str(manifest.get("bundle_sha256") or "")
    status = "PASSED" if manifest.get("status") == "PASSED" and not failures else "FAILED"
    return {
        "reported": True,
        "status": status,
        "generated_at": manifest.get("generated_at"),
        "bundle_sha256": bundle_sha if len(bundle_sha) == 64 else None,
        "transition_count": manifest.get("transition_count"),
        "retention_limit": (manifest.get("source") or {}).get("retention_limit"),
        "entry_count": (manifest.get("source") or {}).get("entry_count"),
        "source_sha256": (manifest.get("source") or {}).get("sha256"),
        "exports": exports,
        "failures": sorted(set(str(value) for value in failures)),
        "read_only": True,
        "controls_available": False,
    }


def timeline_export_path(reports_root: Path, kind: str) -> Path | None:
    name = EXPORT_NAMES.get(kind)
    if name is None:
        return None
    status = build_timeline_export_status(reports_root)
    item = next((row for row in status["exports"] if row["kind"] == kind.upper()), None)
    return reports_root / "phase_ui_obs5p" / name if item and item["verified"] else None


def _missing() -> dict[str, Any]:
    return {
        "reported": False,
        "status": "WAITING",
        "generated_at": None,
        "bundle_sha256": None,
        "transition_count": 0,
        "retention_limit": None,
        "entry_count": 0,
        "source_sha256": None,
        "exports": [],
        "failures": ["CERTIFICATION_MANIFEST_MISSING"],
        "read_only": True,
        "controls_available": False,
    }
