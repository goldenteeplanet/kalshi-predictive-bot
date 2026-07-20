from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def build_prov14a_repair_preview() -> dict[str, Any]:
    repairs = [
        {
            "path": "forecasting.registry.run_forecast_models",
            "defect": "cloud caller omitted the existing snapshot primary key",
            "exact_repair": "insert_forecast(..., market_snapshot_id=snapshot.id)",
            "historical_backfill": False,
        },
        {
            "path": "forecasting.registry.latest_snapshots_for_model",
            "defect": "weather selection admitted stale status rows after close_time",
            "exact_repair": (
                "require active market and snapshot statuses plus non-null future close_time "
                "for weather_v1/weather_v2"
            ),
            "historical_backfill": False,
        },
    ]
    canonical = json.dumps(repairs, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PROV-14A",
        "mode": "LOCAL_NO_WRITE_EXACT_REPAIR_PREVIEW",
        "database_access": False,
        "database_writes": 0,
        "cloud_runtime_modified": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "fuzzy_matching_used": False,
        "repairs": repairs,
        "guarded_cloud_retry_requires_new_approval": True,
        "summary": {
            "repair_count": len(repairs),
            "future_writes_only": True,
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
        "next_action": (
            "After explicit approval, deploy these exact code changes, run cloud smoke tests, "
            "then perform a newly backed-up bounded PROV-14 certification."
        ),
    }


def write_prov14a_repair_preview(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov14a_exact_snapshot_weather_eligibility_repair_preview.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(build_prov14a_repair_preview(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
