"""Bounded rare-joint scenario generation, mutation, and coverage proof."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json

from scripts.local.phase4mz_adversarial_backtest import run_adversarial_backtest
from scripts.local.phase4nd_microstructure_replay import replay_order
from scripts.local.phase4nf_fee_schedule import fee_envelope
from scripts.local.phase4ng_liquidity_capacity import build_capacity_curves
from scripts.local.phase4nh_portfolio_tail_risk import analyze_portfolio

SCHEMA = "phase4ni.scenario-coverage.v1"
MUTATIONS = ("REMOVE", "INVERT", "DELAY", "CORRELATE", "DUPLICATE", "AMPLIFY")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def generate_scenarios(
    ontology: object,
    *,
    forbidden_combinations: list[list[str]],
    targeted_combinations: list[list[str]],
    maximum_scenarios: int,
) -> dict[str, object]:
    errors: list[str] = []
    required = {
        "market",
        "model",
        "data",
        "execution",
        "portfolio",
        "settlement",
        "infrastructure",
        "operator",
    }
    if not isinstance(ontology, dict) or set(ontology) != required:
        return _generation(["ONTOLOGY_CATEGORIES_MISSING"], [])
    factors = sorted({factor for values in ontology.values() for factor in values})
    factor_category = {
        factor: category for category, values in ontology.items() for factor in values
    }
    forbidden = [frozenset(value) for value in forbidden_combinations]
    candidates = []
    for size in (1, 2, 3):
        candidates.extend(itertools.combinations(factors, size))
    candidates.extend(tuple(sorted(value)) for value in targeted_combinations)
    unique = sorted(set(candidates), key=lambda value: (len(value), value))
    scenarios = []
    for combination in unique:
        selected = frozenset(combination)
        if any(rule.issubset(selected) for rule in forbidden):
            continue
        body = {
            "factors": list(combination),
            "categories": sorted(
                {factor_category.get(factor, "unknown") for factor in combination}
            ),
            "order": len(combination),
            "provenance": [
                {"factor": factor, "category": factor_category.get(factor)}
                for factor in combination
            ],
            "mutation": None,
            "parent_scenario_sha256": None,
        }
        scenarios.append({**body, "scenario_sha256": _digest(body)})
    if len(scenarios) > maximum_scenarios:
        errors.append("COMBINATORIAL_BOUND_EXCEEDED")
        scenarios = scenarios[:maximum_scenarios]
    result = _generation(sorted(set(errors)), scenarios)
    result.update(
        {
            "ontology_factor_count": len(factors),
            "candidate_count": len(unique),
            "bounded_count": len(scenarios),
        }
    )
    result["generation_sha256"] = _digest(result)
    return result


def mutate_scenario(scenario: dict[str, object], mutation: str) -> dict[str, object]:
    if mutation not in MUTATIONS:
        raise ValueError("unknown mutation")
    factors = list(scenario["factors"])
    if mutation == "REMOVE" and factors:
        factors = factors[:-1]
    elif mutation == "INVERT":
        factors = [f"INVERTED_{factor}" for factor in factors]
    elif mutation == "DELAY":
        factors.append("DELAYED_EFFECT")
    elif mutation == "CORRELATE":
        factors.append("CORRELATED_LOSS")
    elif mutation == "DUPLICATE" and factors:
        factors.append(factors[0])
    elif mutation == "AMPLIFY":
        factors.append("AMPLIFIED_SHOCK")
    body = {
        "factors": factors,
        "categories": scenario.get("categories"),
        "order": len(factors),
        "provenance": scenario.get("provenance"),
        "mutation": mutation,
        "parent_scenario_sha256": scenario.get("scenario_sha256"),
    }
    return {**body, "scenario_sha256": _digest(body)}


def execute_scenario(scenario: dict[str, object], fixtures: dict[str, object]) -> dict[str, object]:
    factors = set(scenario.get("factors", []))
    components = {}
    backtest_scenario = (
        "FUTURE_DATA_INJECTION"
        if "FUTURE_DATA" in factors
        else "EXTREME_MOVE"
        if "MODEL_INVERSION" in factors
        else "BASELINE"
    )
    components["backtest"] = run_adversarial_backtest(
        fixtures["backtest_records"], scenario=backtest_scenario
    )
    order = copy.deepcopy(fixtures["order"])
    events = copy.deepcopy(fixtures["events"])
    if "STALE_BOOK" in factors or "DELAYED_EFFECT" in factors:
        order["latency_ms"] = 5000
    components["microstructure"] = replay_order(events, order, outcome="yes")
    schedules = copy.deepcopy(fixtures["schedules"])
    if "FEE_SPIKE" in factors or "AMPLIFIED_SHOCK" in factors:
        schedules[0]["taker_rate"] = "1.00"
    components["fees"] = fee_envelope(fixtures["trade"], schedules)
    capacity_args = copy.deepcopy(fixtures["capacity_args"])
    if "DEPTH_WITHDRAWAL" in factors:
        capacity_args["withdrawal_haircut"] = "0.95"
    components["liquidity"] = build_capacity_curves(fixtures["book"], **capacity_args)
    proposed = copy.deepcopy(fixtures["proposed"])
    if "CORRELATED_LOSS" in factors:
        proposed.append(copy.deepcopy(proposed[0]))
        proposed[-1]["position_id"] = "correlated-copy"
        proposed[-1]["economic_exposure_signature"] = proposed[0]["economic_exposure_signature"]
    components["portfolio"] = analyze_portfolio(
        fixtures["snapshot"],
        proposed,
        fixtures["portfolio_scenarios"],
        **fixtures["portfolio_args"],
    )
    refusal_paths = sorted(
        name
        for name, value in components.items()
        if value.get("verdict") == "REFUSE" or value.get("readiness") == "REFUSE"
    )
    result = {
        "scenario_sha256": scenario["scenario_sha256"],
        "factors": scenario["factors"],
        "components": components,
        "refusal_paths": refusal_paths,
        "safety_mutation_refused": bool(refusal_paths),
        "safety": _safety(),
    }
    result["execution_sha256"] = _digest(result)
    return result


def audit_coverage(
    scenarios: list[dict[str, object]],
    executions: list[dict[str, object]],
    *,
    critical_interactions: list[list[str]],
    expected_refusal_mutations: list[str],
) -> dict[str, object]:
    covered_factors = sorted({factor for scenario in scenarios for factor in scenario["factors"]})
    covered_pairs = {
        tuple(sorted(pair))
        for scenario in scenarios
        for pair in itertools.combinations(set(scenario["factors"]), 2)
    }
    covered_triples = {
        tuple(sorted(triple))
        for scenario in scenarios
        for triple in itertools.combinations(set(scenario["factors"]), 3)
    }
    missing_critical = sorted(
        [
            sorted(value)
            for value in critical_interactions
            if tuple(sorted(value)) not in covered_pairs
            and tuple(sorted(value)) not in covered_triples
        ]
    )
    executed = {row["scenario_sha256"]: row for row in executions}
    mutation_survivors = []
    refusal_paths = set()
    for scenario in scenarios:
        execution = executed.get(scenario["scenario_sha256"])
        if execution:
            refusal_paths.update(execution["refusal_paths"])
            if (
                scenario.get("mutation") in expected_refusal_mutations
                and not execution["safety_mutation_refused"]
            ):
                mutation_survivors.append(scenario["scenario_sha256"])
    errors = []
    if missing_critical:
        errors.append("CRITICAL_INTERACTIONS_UNCOVERED")
    if mutation_survivors:
        errors.append("SAFETY_MUTATION_SURVIVED")
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "factor_count": len(covered_factors),
        "pair_count": len(covered_pairs),
        "triple_count": len(covered_triples),
        "covered_factors": covered_factors,
        "missing_critical_interactions": missing_critical,
        "refusal_paths": sorted(refusal_paths),
        "mutation_survivors": mutation_survivors,
        "safety": _safety(),
    }
    result["coverage_sha256"] = _digest(result)
    return result


def minimize_failing_scenario(
    scenario: dict[str, object], fixtures: dict[str, object]
) -> dict[str, object]:
    factors = list(scenario["factors"])
    changed = True
    while changed:
        changed = False
        for factor in list(factors):
            candidate_factors = [value for value in factors if value != factor]
            body = {**scenario, "factors": candidate_factors}
            body_without_hash = {
                key: value for key, value in body.items() if key != "scenario_sha256"
            }
            candidate = {**body_without_hash, "scenario_sha256": _digest(body_without_hash)}
            if execute_scenario(candidate, fixtures)["safety_mutation_refused"]:
                factors = candidate_factors
                changed = True
                break
    result = {
        "original_scenario_sha256": scenario["scenario_sha256"],
        "minimal_factors": factors,
        "minimal_factor_count": len(factors),
        "provenance_preserved": scenario.get("provenance"),
        "safety": _safety(),
    }
    result["minimization_sha256"] = _digest(result)
    return result


def _generation(errors, scenarios):
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "scenarios": scenarios,
        "safety": _safety(),
    }


def _safety():
    return {
        "offline_only": True,
        "order_creation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_execution": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
