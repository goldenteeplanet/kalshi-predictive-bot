from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REQUIRED_MODELS = ("crypto_v2", "weather_v2")
MINIMUM_CYCLES = 3


def build_prov14c_stability_census(
    report_paths: Iterable[Path], *, synthetic_preview: bool = False
) -> dict[str, Any]:
    paths = [Path(path) for path in report_paths]
    cycles: list[dict[str, Any]] = []
    seen_boundaries: set[int] = set()
    seen_event_ids: set[int] = set()
    previous_boundary: int | None = None

    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        summary = payload.get("summary", {})
        boundary = int(payload.get("boundary", {}).get("after_event_id", -1))
        rows = payload.get("rows", [])
        event_ids = [int(row.get("event_id", -1)) for row in rows]
        model_counts = Counter(str(row.get("model_name")) for row in rows)
        failures: list[str] = []

        if payload.get("phase") != "PROV-14":
            failures.append("SOURCE_PHASE_INVALID")
        if summary.get("certification_passed") is not True:
            failures.append("SOURCE_CERTIFICATION_FAILED")
        if payload.get("guardrails", {}).get("execution_enabled") is not False:
            failures.append("EXECUTION_NOT_PROVEN_DISABLED")
        if boundary < 0 or boundary in seen_boundaries:
            failures.append("BOUNDARY_NOT_DISTINCT")
        if previous_boundary is not None and boundary <= previous_boundary:
            failures.append("BOUNDARY_NOT_STRICTLY_INCREASING")
        if not rows:
            failures.append("NO_ATTRIBUTION_EVENTS")
        if len(set(event_ids)) != len(event_ids):
            failures.append("EVENT_ID_DUPLICATE_WITHIN_CYCLE")
        if any(event_id <= boundary for event_id in event_ids):
            failures.append("EVENT_NOT_AFTER_BOUNDARY")
        if seen_event_ids.intersection(event_ids):
            failures.append("EVENT_OVERLAP_ACROSS_CYCLES")
        for model in REQUIRED_MODELS:
            if model_counts[model] == 0:
                failures.append(f"MODEL_MISSING:{model}")
        unexpected_models = sorted(set(model_counts) - set(REQUIRED_MODELS))
        if unexpected_models:
            failures.append("UNEXPECTED_MODEL:" + ",".join(unexpected_models))
        for row in rows:
            if row.get("passed") is not True or row.get("failures"):
                failures.append("EVENT_ATTRIBUTION_FAILED")
            if not row.get("source_observation_ref"):
                failures.append("OBSERVATION_REFERENCE_MISSING")
            if not row.get("market_snapshot_id"):
                failures.append("SNAPSHOT_REFERENCE_MISSING")
            if not row.get("feature_source_table") or not row.get("feature_source_id"):
                failures.append("FEATURE_REFERENCE_MISSING")

        failures = sorted(set(failures))
        cycles.append(
            {
                "source_path": str(path),
                "after_event_id": boundary,
                "event_ids": event_ids,
                "model_counts": dict(sorted(model_counts.items())),
                "events_examined": len(rows),
                "passed": not failures,
                "failures": failures,
            }
        )
        seen_boundaries.add(boundary)
        seen_event_ids.update(event_ids)
        previous_boundary = boundary

    gates = {
        "minimum_three_distinct_cycles": len(cycles) >= MINIMUM_CYCLES,
        "all_cycles_passed": bool(cycles) and all(cycle["passed"] for cycle in cycles),
        "all_cycles_cover_crypto_and_weather": bool(cycles)
        and all(
            all(cycle["model_counts"].get(model, 0) > 0 for model in REQUIRED_MODELS)
            for cycle in cycles
        ),
        "event_ids_do_not_overlap": len(seen_event_ids)
        == sum(len(cycle["event_ids"]) for cycle in cycles),
        "boundaries_strictly_increase": bool(cycles)
        and all(
            cycles[index]["after_event_id"] < cycles[index + 1]["after_event_id"]
            for index in range(len(cycles) - 1)
        ),
        "only_expected_models_present": bool(cycles)
        and all(set(cycle["model_counts"]) == set(REQUIRED_MODELS) for cycle in cycles),
    }
    canonical = json.dumps(cycles, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "PROV-14C",
        "mode": (
            "LOCAL_SYNTHETIC_MULTI_CYCLE_ATTRIBUTION_STABILITY_PREVIEW"
            if synthetic_preview
            else "READ_ONLY_MULTI_CYCLE_ATTRIBUTION_STABILITY_CENSUS"
        ),
        "evidence_kind": "synthetic_fixture" if synthetic_preview else "runtime_export",
        "database_access": False,
        "database_writes": 0,
        "thresholds_changed": False,
        "execution_enabled": False,
        "requirements": {
            "minimum_distinct_cycles": MINIMUM_CYCLES,
            "required_models_each_cycle": list(REQUIRED_MODELS),
        },
        "cycles": cycles,
        "gates": gates,
        "summary": {
            "cycles": len(cycles),
            "events": sum(cycle["events_examined"] for cycle in cycles),
            "stability_census_passed": all(gates.values()),
            "runtime_stability_certified": all(gates.values()) and not synthetic_preview,
            "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def write_prov14c_stability_census(
    report_paths: Iterable[Path],
    output_dir: Path,
    *,
    synthetic_preview: bool = False,
) -> Path:
    payload = build_prov14c_stability_census(report_paths, synthetic_preview=synthetic_preview)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "prov14c_multi_cycle_attribution_stability_census.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
