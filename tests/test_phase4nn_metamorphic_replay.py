from __future__ import annotations

import copy

from scripts.local.phase4ni_scenario_coverage import generate_scenarios
from scripts.local.phase4nn_metamorphic_replay import (
    TRANSFORMATIONS,
    apply_transformation,
    evaluate_transformation,
    run_metamorphic_suite,
)
from tests.test_phase4mz_adversarial_backtest import _records
from tests.test_phase4ni_scenario_coverage import ONTOLOGY, _fixtures


def _joint_scenarios():
    generated = generate_scenarios(
        ONTOLOGY,
        forbidden_combinations=[["FUTURE_DATA", "SETTLEMENT_DELAY"]],
        targeted_combinations=[["STALE_BOOK", "FEE_SPIKE", "CORRELATED_LOSS"]],
        maximum_scenarios=128,
    )["scenarios"]
    wanted = (
        {"FUTURE_DATA", "STALE_BOOK"},
        {"CORRELATED_LOSS", "DEPTH_WITHDRAWAL"},
        {"STALE_BOOK", "FEE_SPIKE", "CORRELATED_LOSS"},
    )
    return [next(row for row in generated if set(row["factors"]) == factors) for factors in wanted]


def test_declared_equivalent_transformations_preserve_required_layers() -> None:
    for name, declaration in TRANSFORMATIONS.items():
        result = evaluate_transformation(_records(), name)
        assert result["verdict"] == "PASS", (name, result["errors"])
        assert result["provenance"]["transformation"] == name
        if declaration["replay"] == "SAME":
            assert result["baseline"]["metrics"] == result["transformed"]["metrics"]


def test_meaningful_and_metadata_changes_affect_only_declared_layers() -> None:
    metadata = evaluate_transformation(_records(), "HARMLESS_METADATA_REMOVAL")
    assert metadata["baseline"]["canonical"] != metadata["transformed"]["canonical"]
    assert metadata["baseline"]["replay"] == metadata["transformed"]["replay"]
    outcome = evaluate_transformation(_records(), "OUTCOME_FLIP")
    assert outcome["baseline"]["replay"] != outcome["transformed"]["replay"]


def test_suite_covers_all_scenarios_and_representative_joint_scenarios() -> None:
    result = run_metamorphic_suite(
        _records(), joint_scenarios=_joint_scenarios(), joint_fixtures=_fixtures()
    )
    assert result["verdict"] == "PASS"
    assert result["scenario_count"] == 17
    assert result["transformation_count"] == len(TRANSFORMATIONS) == 9
    assert result["joint_scenario_count"] == 3


def test_undeclared_transformation_and_missing_joint_fixtures_refuse() -> None:
    result = evaluate_transformation(_records(), "UNKNOWN")
    assert result["verdict"] == "REFUSE"
    assert "UNDECLARED_TRANSFORMATION" in result["errors"]
    missing = run_metamorphic_suite(_records(), joint_scenarios=_joint_scenarios())
    assert "JOINT_FIXTURES_MISSING" in missing["errors"]


def test_normalization_is_idempotent_and_transform_order_independent() -> None:
    first = apply_transformation(
        apply_transformation(_records(), "KEY_REORDER"), "UTC_OFFSET_EQUIVALENT"
    )
    second = apply_transformation(
        apply_transformation(_records(), "UTC_OFFSET_EQUIVALENT"), "KEY_REORDER"
    )
    assert (
        run_metamorphic_suite(first)["suite_sha256"]
        == run_metamorphic_suite(second)["suite_sha256"]
    )


def test_input_and_safety_are_unchanged() -> None:
    records = _records()
    original = copy.deepcopy(records)
    result = run_metamorphic_suite(records)
    assert records == original
    safety = result["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
