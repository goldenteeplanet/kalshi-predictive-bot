from __future__ import annotations

import copy

from scripts.local.phase4ni_scenario_coverage import (
    MUTATIONS,
    audit_coverage,
    execute_scenario,
    generate_scenarios,
    minimize_failing_scenario,
    mutate_scenario,
)
from tests.test_phase4mz_adversarial_backtest import _records
from tests.test_phase4nd_microstructure_replay import _events, _order
from tests.test_phase4nf_fee_schedule import _schedule, _trade
from tests.test_phase4ng_liquidity_capacity import _book
from tests.test_phase4nh_portfolio_tail_risk import _position, _scenarios, _snapshot

ONTOLOGY = {
    "market": ["REGIME_SHIFT"],
    "model": ["MODEL_INVERSION"],
    "data": ["FUTURE_DATA"],
    "execution": ["STALE_BOOK"],
    "portfolio": ["CORRELATED_LOSS"],
    "settlement": ["SETTLEMENT_DELAY"],
    "infrastructure": ["DEPTH_WITHDRAWAL"],
    "operator": ["FEE_SPIKE"],
}


def _fixtures():
    return {
        "backtest_records": _records(),
        "events": _events(),
        "order": _order(max_book_age_ms=100),
        "schedules": [_schedule()],
        "trade": _trade(),
        "book": _book(),
        "capacity_args": {
            "decision_time": "2026-08-01T12:00:00Z",
            "requested_sizes": [1, 5, 10],
            "yes_probability": "0.65",
            "fee_per_contract": "0.02",
            "maximum_book_age_ms": 200,
            "price_cap": "0.70",
            "tick_size": "0.01",
            "withdrawal_haircut": "0.30",
            "queue_ahead": "1",
            "correlated_demand": "2",
            "self_impact_coefficient": "0.04",
        },
        "snapshot": _snapshot(),
        "proposed": [_position("proposed", "crypto", 1)],
        "portfolio_scenarios": _scenarios(),
        "portfolio_args": {
            "evaluated_at": "2026-08-01T12:00:00Z",
            "maximum_snapshot_age_seconds": 60,
            "maximum_joint_loss": "5",
            "maximum_concentration": "0.80",
        },
    }


def _generate(maximum=128):
    return generate_scenarios(
        ONTOLOGY,
        forbidden_combinations=[["FUTURE_DATA", "SETTLEMENT_DELAY"]],
        targeted_combinations=[["STALE_BOOK", "FEE_SPIKE", "CORRELATED_LOSS", "DEPTH_WITHDRAWAL"]],
        maximum_scenarios=maximum,
    )


def test_generation_is_deterministic_bounded_and_preserves_provenance() -> None:
    first = _generate()
    assert first == _generate()
    assert first["verdict"] == "PASS"
    assert first["bounded_count"] <= 128
    assert all(scenario["provenance"] for scenario in first["scenarios"])


def test_missing_ontology_category_and_combinatorial_bound_refuse() -> None:
    missing = copy.deepcopy(ONTOLOGY)
    missing.pop("operator")
    assert (
        "ONTOLOGY_CATEGORIES_MISSING"
        in generate_scenarios(
            missing, forbidden_combinations=[], targeted_combinations=[], maximum_scenarios=10
        )["errors"]
    )
    assert "COMBINATORIAL_BOUND_EXCEEDED" in _generate(maximum=5)["errors"]


def test_impossible_combinations_are_excluded_and_duplicates_deduplicated() -> None:
    result = generate_scenarios(
        ONTOLOGY,
        forbidden_combinations=[["FUTURE_DATA", "SETTLEMENT_DELAY"]],
        targeted_combinations=[["STALE_BOOK", "FEE_SPIKE"], ["FEE_SPIKE", "STALE_BOOK"]],
        maximum_scenarios=128,
    )
    factor_sets = [tuple(row["factors"]) for row in result["scenarios"]]
    assert len(factor_sets) == len(set(factor_sets))
    assert not any(
        {"FUTURE_DATA", "SETTLEMENT_DELAY"}.issubset(set(values)) for values in factor_sets
    )


def test_all_mutations_are_deterministic_and_parent_bound() -> None:
    parent = _generate()["scenarios"][0]
    for mutation in MUTATIONS:
        first = mutate_scenario(parent, mutation)
        assert first == mutate_scenario(parent, mutation)
        assert first["parent_scenario_sha256"] == parent["scenario_sha256"]
        assert first["mutation"] == mutation


def test_safety_factors_feed_real_prior_models_and_refuse() -> None:
    scenarios = _generate()["scenarios"]
    for factor, component in (
        ("FUTURE_DATA", "backtest"),
        ("STALE_BOOK", "microstructure"),
        ("CORRELATED_LOSS", "portfolio"),
    ):
        scenario = next(row for row in scenarios if row["factors"] == [factor])
        execution = execute_scenario(scenario, _fixtures())
        assert component in execution["refusal_paths"]


def test_coverage_reports_factors_interactions_and_refusal_paths() -> None:
    scenarios = _generate()["scenarios"]
    selected = [row for row in scenarios if len(row["factors"]) <= 2]
    executions = [execute_scenario(row, _fixtures()) for row in selected]
    coverage = audit_coverage(
        selected,
        executions,
        critical_interactions=[
            ["STALE_BOOK", "FEE_SPIKE"],
            ["CORRELATED_LOSS", "DEPTH_WITHDRAWAL"],
        ],
        expected_refusal_mutations=[],
    )
    assert coverage["verdict"] == "PASS"
    assert coverage["factor_count"] == 8
    assert coverage["pair_count"] > 0
    assert {"backtest", "microstructure", "portfolio"}.issubset(set(coverage["refusal_paths"]))


def test_missing_critical_interaction_and_surviving_mutation_refuse_certification() -> None:
    base = _generate()["scenarios"][0]
    survivor = mutate_scenario(base, "REMOVE")
    execution = execute_scenario(survivor, _fixtures())
    coverage = audit_coverage(
        [survivor],
        [execution],
        critical_interactions=[["STALE_BOOK", "FEE_SPIKE"]],
        expected_refusal_mutations=["REMOVE"],
    )
    assert coverage["verdict"] == "REFUSE"
    assert "CRITICAL_INTERACTIONS_UNCOVERED" in coverage["errors"]
    assert "SAFETY_MUTATION_SURVIVED" in coverage["errors"]


def test_failing_scenario_minimization_retains_causal_factor_and_provenance() -> None:
    scenario = next(
        row
        for row in _generate()["scenarios"]
        if set(row["factors"]) == {"FUTURE_DATA", "STALE_BOOK"}
    )
    result = minimize_failing_scenario(scenario, _fixtures())
    assert result["minimal_factor_count"] == 1
    assert result["minimal_factors"][0] in {"FUTURE_DATA", "STALE_BOOK"}
    assert result["provenance_preserved"] == scenario["provenance"]


def test_scenario_system_has_no_order_or_execution_capability() -> None:
    safety = _generate()["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
