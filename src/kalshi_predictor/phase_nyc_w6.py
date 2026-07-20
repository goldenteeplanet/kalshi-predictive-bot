from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import utc_now


def write_runtime_integration_preview(*, w5_report: Path, output_dir: Path) -> Path:
    w5 = json.loads(w5_report.read_text(encoding="utf-8"))
    summary = w5.get("summary", {})
    ready = summary.get("runtime_activation_ready") is True
    windows = w5.get("windows", [])
    blockers = []
    if not ready:
        blockers.append("NYC_W5_RUNTIME_ACTIVATION_NOT_READY")
    if len(windows) < 3:
        blockers.append("INSUFFICIENT_CERTIFIED_WINDOWS")
    if any(not row.get("alignment_passed") for row in windows):
        blockers.append("OBSERVATION_ALIGNMENT_NOT_EXACT")
    report: dict[str, Any] = {
        "phase": "NYC-W6", "generated_at": utc_now().isoformat(),
        "mode": "GUARDED_RUNTIME_INTEGRATION_PREVIEW_NO_WRITE",
        "database_writes": 0, "execution_enabled": False,
        "runtime_weather_v2_changed": False, "thresholds_changed": False,
        "source_policy": {
            "observation_source": "NOAA KNYC",
            "role": "NON_SETTLEMENT_POINT_OBSERVATION_EVIDENCE",
            "settlement_source_remains": "The Weather Company",
            "allowed_station": "KNYC", "maximum_alignment_minutes": 15,
            "fuzzy_location_matching": False, "fuzzy_target_time_matching": False,
        },
        "proposed_runtime_path": [
            "Require exact KXTEMPNYCH ticker metadata validation.",
            "Load only exact new_york target-time weather features.",
            "Attach KNYC observation only when station and offset gates pass.",
            "Record NOAA evidence separately from settlement provenance.",
            "Apply the existing bounded weather_v2 adjustment without threshold changes.",
            "Fall back to current weather_v2 when exact observation evidence is unavailable.",
        ],
        "rollback_switch": "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false",
        "activation_preview_ready": not blockers,
        "blockers": blockers,
        "evidence": {
            "certified_windows": summary.get("certified_windows"),
            "settled_windows": summary.get("settled_windows"),
            "mean_absolute_divergence_f": summary.get("mean_absolute_divergence_f"),
            "maximum_absolute_divergence_f": summary.get("maximum_absolute_divergence_f"),
        },
        "next_gate": (
            "Implement shadow-only runtime wiring behind the disabled feature flag."
            if not blockers else "Resolve NYC-W5 blockers before runtime wiring."
        ),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "nyc_w6_weather_observation_runtime_integration_preview.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path
