from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.drawdown_guard import write_drawdown_aware_guard_refinement
from kalshi_predictor.benchmarking.exposure_bundle import (
    golden_exposure_bundle_summary,
    write_exposure_guard_certification_bundle,
)
from kalshi_predictor.benchmarking.exposure_stability import (
    write_multi_seed_exposure_stability_census,
)
from kalshi_predictor.benchmarking.interaction_boundary import (
    write_interaction_boundary_refinement,
)
from kalshi_predictor.benchmarking.oos_exposure_guard import (
    write_oos_exposure_guard_validation,
)
from kalshi_predictor.benchmarking.stress_guard import (
    write_stress_aware_allocation_guard_preview,
)


DEFAULT_GOLDEN = Path("tests/golden/pmb27_exposure_guard_bundle_summary.json")


def run_offline_exposure_certification_ci_gate(
    project_root: Path,
    output_dir: Path,
    *,
    golden_path: Path | None = None,
) -> tuple[Path, int]:
    root = output_dir / "regenerated"
    write_interaction_boundary_refinement(root / "reports/phase_pmb22")
    write_stress_aware_allocation_guard_preview(root / "reports/phase_pmb23")
    write_drawdown_aware_guard_refinement(root / "reports/phase_pmb24")
    write_oos_exposure_guard_validation(root / "reports/phase_pmb25")
    write_multi_seed_exposure_stability_census(root / "reports/phase_pmb26")
    bundle_path = write_exposure_guard_certification_bundle(
        root, root / "reports/phase_pmb27"
    )
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    selected_golden = golden_path or (project_root / DEFAULT_GOLDEN)
    expected = json.loads(selected_golden.read_text(encoding="utf-8"))
    actual = golden_exposure_bundle_summary(bundle)
    golden_match = actual == expected
    artifact_hashes_match = actual.get("artifact_hashes") == expected.get("artifact_hashes")
    bundle_digest_match = actual.get("bundle_digest") == expected.get("bundle_digest")
    certification_passed = bundle["certification"]["passed"] is True
    passed = (
        golden_match and artifact_hashes_match and bundle_digest_match
        and certification_passed
    )
    canonical = json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()
    report: dict[str, Any] = {
        "phase": "PMB-28",
        "mode": "LOCAL_OFFLINE_CERTIFICATION_CI_GATE",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "policy_activated": False,
        "runtime_activation_authorized": False,
        "checks": {
            "regenerated_certification_passed": certification_passed,
            "golden_summary_match": golden_match,
            "artifact_hashes_match": artifact_hashes_match,
            "bundle_digest_match": bundle_digest_match,
        },
        "summary": {
            "passed": passed,
            "exit_code": 0 if passed else 1,
            "regenerated_artifact_count": bundle["summary"]["artifact_count"],
            "regenerated_bundle_digest": bundle["summary"]["bundle_digest"],
            "golden_digest": expected.get("bundle_digest"),
            "actual_summary_digest": hashlib.sha256(canonical).hexdigest(),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb28_offline_certification_ci_gate.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path, report["summary"]["exit_code"]
