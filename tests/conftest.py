from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kalshi_predictor.benchmarking.drawdown_guard import (
    write_drawdown_aware_guard_refinement,
)
from kalshi_predictor.benchmarking.exposure_bundle import (
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


@pytest.fixture(scope="session")
def pmb_release_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build deterministic PMB release evidence without relying on ignored reports."""
    source_root = Path(__file__).resolve().parents[1]
    root = tmp_path_factory.mktemp("pmb_release_root")
    tracked_inputs = (
        Path("pyproject.toml"),
        Path(".github/workflows/pmb28-offline-certification.yml"),
        Path("tests/golden/pmb22_interaction_boundary_summary.json"),
        Path("tests/golden/pmb27_exposure_guard_bundle_summary.json"),
    )
    for relative in tracked_inputs:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / relative, destination)

    write_interaction_boundary_refinement(root / "reports/phase_pmb22")
    write_stress_aware_allocation_guard_preview(root / "reports/phase_pmb23")
    write_drawdown_aware_guard_refinement(root / "reports/phase_pmb24")
    write_oos_exposure_guard_validation(root / "reports/phase_pmb25")
    write_multi_seed_exposure_stability_census(root / "reports/phase_pmb26")
    write_exposure_guard_certification_bundle(root, root / "reports/phase_pmb27")

    release_guards = {
        28: {
            "phase": "PMB-28",
            "execution_enabled": False,
            "database_writes": 0,
            "summary": {"passed": True},
        },
        29: {"phase": "PMB-29", "execution_enabled": False, "database_writes": 0},
        30: {"phase": "PMB-30", "execution_enabled": False, "database_writes": 0},
        31: {"phase": "PMB-31", "execution_enabled": False, "database_writes": 0},
    }
    names = {
        28: "pmb28_offline_certification_ci_gate.json",
        29: "pmb29_local_ci_workflow_integration_preview.json",
        30: "pmb30_offline_ci_failure_mode_matrix.json",
        31: "pmb31_cross_environment_reproducibility_preview.json",
    }
    for number, payload in release_guards.items():
        path = root / f"reports/phase_pmb{number}" / names[number]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return root
