from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.observation_shadow import evaluate_knyc_observation
from kalshi_predictor.weather.temperature_contracts import parse_point_temperature_ticker


def write_shadow_runtime_report(*, reports_dir: Path, output_dir: Path,
                                max_adjustment: Decimal,
                                source_paths: list[Path] | None = None) -> Path:
    rows = []
    paths = source_paths or sorted(
        reports_dir.glob("phase_nyc_w4*/nyc_w4_observation_feature_integration_preview.json")
    )
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for source in payload.get("rows", []):
            if not source.get("preview_passed"):
                continue
            contract = parse_point_temperature_ticker(str(source.get("ticker") or ""))
            if contract is None:
                continue
            baseline = Decimal(str(source["baseline_probability_without_observation"]))
            result = evaluate_knyc_observation(
                baseline_probability=baseline, raw_strike=contract.raw_strike,
                target_time=source["target_utc_time"], max_adjustment=max_adjustment,
                enabled=False, evidence={
                    "station_id": "KNYC", "evidence_role": "NON_SETTLEMENT_POINT_OBSERVATION",
                    "settlement_source": "the_weather_company",
                    "target_utc_time": source["target_utc_time"],
                    "offset_seconds": source["observation_offset_seconds"],
                    "observation_temperature_f": source["observation_temperature_f"],
                },
            )
            rows.append({
                "ticker": source["ticker"], "passed": result.passed,
                "runtime_applied": result.applied,
                "target_utc_time": source["target_utc_time"],
                "baseline_probability": str(baseline),
                "runtime_probability": str(result.applied_probability),
                "shadow_probability": str(result.shadow_probability),
                "shadow_change": str(result.shadow_probability - baseline),
                "provenance": result.provenance,
            })
    report = {
        "phase": "NYC-W7", "generated_at": utc_now().isoformat(),
        "mode": "SHADOW_ONLY_DISABLED_FEATURE_FLAG",
        "feature_flag": "WEATHER_V2_KNYC_OBSERVATION_ENABLED=false",
        "database_writes": 0, "execution_enabled": False,
        "runtime_weather_v2_changed": False, "thresholds_changed": False,
        "rows": rows,
        "summary": {"shadow_rows": len(rows), "passed": sum(row["passed"] for row in rows),
                    "runtime_applied": sum(row["runtime_applied"] for row in rows),
                    "rollback_verified": all(not row["runtime_applied"] for row in rows)},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "nyc_w7_shadow_observation_runtime_report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path
