"""Local preview for exact partial-market metadata preservation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_prov14b_r4_preview() -> dict[str, Any]:
    return {
        "phase": "PROV-14B-R4",
        "status": "PASSED_LOCAL_PREVIEW",
        "mode": "LOCAL_SYNTHETIC_NO_WRITE_PREVIEW",
        "root_cause": "PARTIAL_SNAPSHOT_UPSERT_ERASES_MARKET_CLOSE_TIME",
        "repair": {
            "scope": "insert_market_snapshot only",
            "preserved_field": "Market.close_time",
            "preserve_when": "close_time key is omitted and an exact stored value exists",
            "explicit_value_semantics_unchanged": True,
            "new_market_without_close_time_fails_closed": True,
            "selector_predicates_changed": False,
            "freshness_threshold_changed": False,
            "target_time_tolerance_changed": False,
            "fuzzy_matching_added": False,
        },
        "certification_requirements": [
            "omitted close_time preserves the existing exact value",
            "explicit replacement close_time is applied",
            "explicit null retains existing upsert semantics",
            "new partial markets remain ineligible",
            "current exact weather rows pass the unchanged selector",
        ],
        "guardrails": {
            "database_access": False,
            "database_writes": 0,
            "cloud_runtime_modified": False,
            "execution_enabled": False,
            "guarded_cloud_retry_requires_new_approval": True,
        },
        "next_phase": (
            "PROV-14B-R5 — Guarded Cloud Metadata-Preservation Deployment "
            "and Attribution Certification Retry"
        ),
    }


def write_prov14b_r4_preview(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov14b_r4_partial_market_metadata_preservation_preview.json"
    path.write_text(
        json.dumps(build_prov14b_r4_preview(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
