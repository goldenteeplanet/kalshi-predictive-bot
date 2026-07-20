from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

H_REPORT = Path("ui_obs2h/ui_obs2h_notification_pipeline_ci.json")
I_REPORT = Path("ui_obs2i/ui_obs2i_local_ci_workflow_preview.json")
HISTORY_LIMIT = 10
MAX_CERTIFICATION_REPORT_BYTES = 1_048_576


def _load(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if path.stat().st_size > MAX_CERTIFICATION_REPORT_BYTES:
            return None, "TOO_LARGE"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "MISSING"
    except (OSError, json.JSONDecodeError):
        return None, "INVALID"
    return payload if isinstance(payload, dict) else None, None if isinstance(
        payload, dict
    ) else "INVALID"


def _sha(path: Path) -> str | None:
    if not path.is_file() or path.stat().st_size > MAX_CERTIFICATION_REPORT_BYTES:
        return None
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _entry(
    path: Path, payload: dict[str, Any], *, display_path: Path | None = None
) -> dict[str, Any]:
    bundle = payload.get("bundle") or {}
    diagnostics = list(payload.get("diagnostics") or bundle.get("diagnostics") or [])
    return {
        "phase": payload.get("phase") or "UNKNOWN",
        "status": payload.get("status") or "FAILED",
        "path": (display_path or path).as_posix(),
        "sha256": _sha(path),
        "bundle_digest": bundle.get("bundle_digest"),
        "workflow_sha256": payload.get("workflow_sha256"),
        "generated_at": payload.get("generated_at") or "not recorded",
        "drift_detected": "GOLDEN_DRIFT_DETECTED" in diagnostics,
        "diagnostics": diagnostics,
    }


def build_ci_certification_status(reports_root: Path) -> dict[str, Any]:
    h_path, i_path = reports_root / H_REPORT, reports_root / I_REPORT
    h, h_error = _load(h_path)
    i, i_error = _load(i_path)
    diagnostics = []
    if h_error:
        diagnostics.append(f"UI_OBS_2H_REPORT_{h_error}")
    if i_error:
        diagnostics.append(f"UI_OBS_2I_REPORT_{i_error}")
    history_paths = []
    history_dir = reports_root / "ui_obs2h/history"
    if history_dir.is_dir():
        history_paths.extend(sorted(history_dir.glob("*.json"), reverse=True))
    history_paths.extend(path for path in (h_path, i_path) if path.is_file())
    seen: set[Path] = set()
    history = []
    for path in history_paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        payload, error = _load(path)
        if payload is not None:
            history.append(_entry(path, payload, display_path=path.relative_to(reports_root)))
        elif error:
            diagnostics.append(f"CERTIFICATION_HISTORY_{error}:{path.name}")
        if len(history) >= HISTORY_LIMIT:
            break
    h_entry = _entry(h_path, h, display_path=H_REPORT) if h else None
    i_entry = _entry(i_path, i, display_path=I_REPORT) if i else None
    drift_failures = [item for item in history if item["drift_detected"]]
    passed = bool(
        h_entry
        and i_entry
        and h_entry["status"] == "PASSED"
        and i_entry["status"] == "PASSED"
        and not drift_failures
    )
    return {
        "status": "PASSED" if passed else "BLOCKED",
        "read_only": True,
        "gate": h_entry,
        "workflow": i_entry,
        "retention": {"days": 30, "history_limit": HISTORY_LIMIT, "artifact_on_failure": True},
        "drift_failures": drift_failures,
        "history": history,
        "history_count": len(history),
        "diagnostics": diagnostics,
        "cloud_access": False,
        "controls_available": False,
    }
