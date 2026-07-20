import json
from decimal import Decimal

import pytest

from kalshi_predictor.benchmarking.joint_surface import (
    build_joint_robust_decision_surface,
    write_joint_robust_decision_surface,
)


def test_pmb15_is_deterministic_and_builds_81_row_surface(tmp_path):
    first = json.loads(write_joint_robust_decision_surface(tmp_path / "a").read_text())
    second = json.loads(write_joint_robust_decision_surface(tmp_path / "b").read_text())
    assert first == second
    assert first["summary"]["rows"] == 81
    assert first["summary"]["zones"] == 27
    assert first["summary"]["all_attribution_complete"] is True
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False


def test_pmb15_identifies_robust_and_fragile_decision_zones():
    report = build_joint_robust_decision_surface()
    summary = report["summary"]
    assert summary["robust_allocate_zones"] > 0
    assert summary["robust_reject_zones"] > 0
    assert summary["fragile_zones"] > 0
    assert (
        summary["robust_allocate_zones"]
        + summary["robust_reject_zones"]
        + summary["fragile_zones"]
    ) == 27


def test_pmb15_rejects_empty_or_unbounded_forecast_grid():
    with pytest.raises(ValueError, match="non-empty"):
        build_joint_robust_decision_surface(forecast_perturbations=())
    with pytest.raises(ValueError, match="bounded"):
        build_joint_robust_decision_surface(
            forecast_perturbations=(Decimal("0.11"),)
        )
