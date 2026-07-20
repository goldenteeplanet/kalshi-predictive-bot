from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ARTIFACTS = {
    "PMB-22": Path("reports/phase_pmb22/pmb22_interaction_boundary_refinement.json"),
    "PMB-23": Path("reports/phase_pmb23/pmb23_stress_aware_allocation_guard_preview.json"),
    "PMB-24": Path("reports/phase_pmb24/pmb24_drawdown_aware_guard_refinement_preview.json"),
    "PMB-25": Path("reports/phase_pmb25/pmb25_oos_exposure_guard_validation.json"),
    "PMB-26": Path("reports/phase_pmb26/pmb26_multi_seed_oos_exposure_stability_census.json"),
}


def build_exposure_guard_certification_bundle(project_root: Path) -> dict[str, Any]:
    loaded: dict[str, dict[str, Any]] = {}
    artifacts = []
    for phase, relative in ARTIFACTS.items():
        path = project_root / relative
        raw = path.read_bytes()
        payload = json.loads(raw)
        loaded[phase] = payload
        artifacts.append({
            "phase": phase,
            "path": relative.as_posix(),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "declared_phase_matches": payload.get("phase") == phase,
        })
    p22, p23, p24, p25, p26 = (loaded[f"PMB-{number}"] for number in range(22, 27))
    checks = {
        "all_declared_phases_match": all(row["declared_phase_matches"] for row in artifacts),
        "pmb22_buffer_certified": p22["deterministic_safety_buffer"]["certified_within_grid"] is True,
        "pmb22_buffer_is_008_008": (
            p22["deterministic_safety_buffer"]["maximum_forecast_bias_magnitude"] == "0.008"
            and p22["deterministic_safety_buffer"]["maximum_spread_addition"] == "0.008"
        ),
        "pmb23_uses_pmb22_digest": (
            p23["certified_buffer"]["source_digest"] == p22["summary"]["deterministic_digest"]
        ),
        "pmb23_remained_preview_only": p23["runtime_policy_changed"] is False,
        "pmb24_recommends_frozen_095": (
            p24["recommended_preview"] is not None
            and p24["recommended_preview"]["position_scale"] == "0.95"
            and p24["recommended_preview"]["qualifies"] is True
        ),
        "pmb25_uses_frozen_095": p25["frozen_position_scale"] == "0.95",
        "pmb25_oos_validation_passed": p25["summary"]["validation_passed"] is True,
        "pmb26_uses_frozen_095": p26["frozen_position_scale"] == "0.95",
        "pmb26_all_seeds_passed": p26["summary"]["all_seeds_passed"] is True,
        "all_phases_execution_disabled": all(
            payload["execution_enabled"] is False for payload in loaded.values()
        ),
        "all_phases_database_write_free": all(
            payload["database_writes"] == 0 for payload in loaded.values()
        ),
        "all_phases_thresholds_unchanged": all(
            payload["thresholds_changed"] is False for payload in loaded.values()
        ),
    }
    certification_passed = all(checks.values())
    canonical = json.dumps(
        {"artifacts": artifacts, "checks": checks}, sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "phase": "PMB-27",
        "mode": "LOCAL_OFFLINE_EXPOSURE_GUARD_GOLDEN_CERTIFICATION_BUNDLE",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "policy_activated": False,
        "artifacts": artifacts,
        "cross_report_checks": checks,
        "certification": {
            "passed": certification_passed,
            "certified_buffer": {"forecast_bias_magnitude": "0.008", "spread_addition": "0.008"},
            "certified_position_scale": "0.95",
            "runtime_activation_authorized": False,
        },
        "summary": {
            "artifact_count": len(artifacts),
            "checks_passed": sum(checks.values()),
            "checks_total": len(checks),
            "bundle_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }


def golden_exposure_bundle_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": bundle["phase"],
        "artifact_hashes": {
            row["phase"]: row["sha256"] for row in bundle["artifacts"]
        },
        "certification": bundle["certification"],
        "checks_passed": bundle["summary"]["checks_passed"],
        "checks_total": bundle["summary"]["checks_total"],
        "bundle_digest": bundle["summary"]["bundle_digest"],
    }


def write_exposure_guard_certification_bundle(
    project_root: Path,
    output_dir: Path,
    *,
    golden_path: Path | None = None,
) -> Path:
    bundle = build_exposure_guard_certification_bundle(project_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb27_exposure_guard_golden_certification_bundle.json"
    _atomic_json(path, bundle)
    if golden_path is not None:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(golden_path, golden_exposure_bundle_summary(bundle))
    return path


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
