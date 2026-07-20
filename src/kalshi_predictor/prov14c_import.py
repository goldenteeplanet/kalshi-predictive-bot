from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

REQUIRED_ROW_FIELDS = (
    "event_id", "model_name", "source_observation_ref", "market_snapshot_id",
    "feature_source_table", "feature_source_id", "passed", "failures",
)


def import_runtime_attribution_exports(paths: list[Path]) -> dict[str, Any]:
    imported: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_boundaries: set[int] = set()
    for path in sorted(map(Path, paths), key=lambda value: str(value)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            rejected.append({"path": str(path), "diagnostics": [f"EXPORT_INVALID:{type(exc).__name__}"]})
            continue
        diagnostics: list[str] = []
        boundary = payload.get("boundary", {}).get("after_event_id")
        if not isinstance(boundary, int):
            diagnostics.append("BOUNDARY_MISSING_OR_INVALID")
        elif boundary in seen_boundaries:
            diagnostics.append("BOUNDARY_DUPLICATE")
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            diagnostics.append("ROWS_MISSING_OR_EMPTY")
            rows = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                diagnostics.append(f"ROW_NOT_OBJECT:{index}")
                continue
            for field in REQUIRED_ROW_FIELDS:
                if field not in row or row[field] in (None, ""):
                    diagnostics.append(f"ROW_FIELD_MISSING:{index}:{field}")
        if payload.get("guardrails", {}).get("execution_enabled") is not False:
            diagnostics.append("EXECUTION_DISABLED_NOT_PROVEN")
        if diagnostics:
            rejected.append({"path": str(path), "diagnostics": sorted(set(diagnostics))})
            continue
        seen_boundaries.add(boundary)
        normalized = {
            "phase": "PROV-14",
            "boundary": {"after_event_id": boundary},
            "summary": {"certification_passed": payload.get("summary", {}).get("certification_passed") is True},
            "guardrails": {"execution_enabled": False},
            "rows": rows,
        }
        canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        imported.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "normalized": normalized, "normalized_sha256": hashlib.sha256(canonical).hexdigest()})
    return {
        "phase": "PROV-14C-A",
        "mode": "LOCAL_READ_ONLY_RUNTIME_EXPORT_IMPORT",
        "database_access": False,
        "database_writes": 0,
        "execution_enabled": False,
        "imported": imported,
        "rejected": rejected,
        "summary": {"inputs": len(paths), "imported": len(imported), "rejected": len(rejected), "ready_for_prov14c": len(imported) >= 3 and not rejected},
    }


def write_import_bundle(paths: list[Path], output: Path) -> Path:
    report = import_runtime_attribution_exports(paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return output
