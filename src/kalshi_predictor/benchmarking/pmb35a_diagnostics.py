from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def build_pmb35a_diagnostics(mapping_path: Path) -> dict[str, Any]:
    source = json.loads(mapping_path.read_text(encoding="utf-8"))
    blocked = []
    for row in source.get("rows", []):
        if row.get("compatible") is True:
            continue
        diagnostics = list(row.get("diagnostics") or [])
        exact_missing = [item for item in diagnostics if "FIELD_MISSING" in item or "CONTEXT_MISSING" in item]
        blocked.append({
            "fixture_id": row.get("fixture_id"),
            "category": row.get("category"),
            "diagnostics": diagnostics,
            "exact_missing_inputs": exact_missing,
            "required_repair": "Provide an independent exact reference forecast probability and distinct current/reference executable-spread snapshots for the same ticker/category/time identity.",
            "defaults_allowed": False,
        })
    priorities = []
    for row in blocked:
        for diagnostic in row["exact_missing_inputs"]:
            priorities.append({"priority": 1 if "REFERENCE_FORECAST" in diagnostic else 2, "diagnostic": diagnostic, "fixture_id": row["fixture_id"]})
    priorities.sort(key=lambda item: (item["priority"], item["diagnostic"], str(item["fixture_id"])))
    canonical = json.dumps(blocked, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PMB-35A",
        "status": "BLOCKED_EXACT_INPUT_REQUIRED" if blocked else "PASSED_READY_FOR_DISABLED_SHADOW",
        "mode": "LOCAL_EXACT_MISSING_REFERENCE_DIAGNOSTICS",
        "cloud_access": False,
        "database_access": False,
        "database_writes": 0,
        "execution_enabled": False,
        "fabricated_or_default_values_allowed": False,
        "source": {"path": str(mapping_path), "pmb35_deployment_unblocked": source.get("summary", {}).get("pmb35_deployment_unblocked") is True},
        "blocked_rows": blocked,
        "repair_priorities": priorities,
        "summary": {"blocked_rows": len(blocked), "exact_missing_inputs": sum(len(row["exact_missing_inputs"]) for row in blocked), "pmb35_deployment_unblocked": not blocked},
        "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
    }


def write_pmb35a_diagnostics(mapping_path: Path, output: Path) -> Path:
    report = build_pmb35a_diagnostics(mapping_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return output
