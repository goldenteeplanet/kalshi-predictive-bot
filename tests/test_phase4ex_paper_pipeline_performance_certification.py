from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = (
    Path(__file__).parents[1] / "scripts/local/phase4ex_paper_pipeline_performance_certification.py"
)
SPEC = importlib.util.spec_from_file_location("phase4ex", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def scenario(scenario_id="A", baseline=1000, optimized=800, equivalent=True):
    return {
        "scenario_id": scenario_id,
        "baseline_latency_us": baseline,
        "optimized_latency_us": optimized,
        "behavior_equivalent": equivalent,
    }


def payload(scenarios=None, minimum=2000):
    return _seal(
        {
            "schema": MODULE.INPUT_SCHEMA,
            "proofs": {
                "risk_paper_equivalence_certified": True,
                "airgapped_acceptance_passed": True,
                "mutation_scanner_advancement_allowed": True,
                "equivalence_artifact_hash": "a" * 64,
                "acceptance_artifact_hash": "b" * 64,
                "scanner_artifact_hash": "c" * 64,
            },
            "minimum_aggregate_improvement_bps": minimum,
            "scenarios": [scenario()] if scenarios is None else scenarios,
        }
    )


def test_exact_improvement_threshold_certifies():
    report = MODULE.build_report(payload())
    assert report["aggregate_improvement_bps"] == 2000
    assert report["paper_pipeline_performance_certified"] is True
    assert report["certification_reason_codes"] == []
    assert report["scenario_results"][0]["passed"] is True


def test_one_basis_point_below_threshold_refuses():
    report = MODULE.build_report(payload(minimum=2001))
    assert report["paper_pipeline_performance_certified"] is False
    assert report["certification_reason_codes"] == ["AGGREGATE_IMPROVEMENT_BELOW_THRESHOLD"]


def test_latency_regression_refuses_even_if_aggregate_improves():
    report = MODULE.build_report(
        payload([scenario("A", 1000, 500), scenario("B", 1000, 1001)], minimum=1000)
    )
    assert report["aggregate_improvement_bps"] > 1000
    assert "SCENARIO_B_LATENCY_REGRESSION" in report["certification_reason_codes"]
    assert report["paper_pipeline_performance_certified"] is False


def test_behavior_drift_refuses():
    report = MODULE.build_report(payload([scenario(equivalent=False)]))
    assert report["certification_reason_codes"] == ["SCENARIO_A_BEHAVIOR_NOT_EQUIVALENT"]
    assert report["paper_pipeline_performance_certified"] is False


@pytest.mark.parametrize(
    ("proof", "reason"),
    [
        ("risk_paper_equivalence_certified", "RISK_PAPER_EQUIVALENCE_PROOF_FAILED"),
        ("airgapped_acceptance_passed", "AIRGAPPED_ACCEPTANCE_PROOF_FAILED"),
        ("mutation_scanner_advancement_allowed", "MUTATION_SCANNER_PROOF_FAILED"),
    ],
)
def test_each_failed_prerequisite_refuses(proof, reason):
    value = payload()
    value["proofs"][proof] = False
    _seal(value)
    report = MODULE.build_report(value)
    assert reason in report["certification_reason_codes"]
    assert report["paper_pipeline_performance_certified"] is False


def test_zero_optimized_latency_is_valid_full_improvement():
    report = MODULE.build_report(payload([scenario(optimized=0)], minimum=10_000))
    assert report["aggregate_improvement_bps"] == 10_000
    assert report["paper_pipeline_performance_certified"] is True


def test_equal_latency_can_certify_zero_threshold():
    report = MODULE.build_report(payload([scenario(optimized=1000)], minimum=0))
    assert report["aggregate_improvement_bps"] == 0
    assert report["paper_pipeline_performance_certified"] is True


def test_integer_aggregate_uses_all_scenarios():
    report = MODULE.build_report(
        payload([scenario("A", 1000, 700), scenario("B", 3000, 2700)], minimum=1500)
    )
    assert report["baseline_total_us"] == 4000
    assert report["optimized_total_us"] == 3400
    assert report["aggregate_improvement_bps"] == 1500


def test_input_order_is_normalized():
    first = MODULE.build_report(payload([scenario("B"), scenario("A")]))
    second = MODULE.build_report(payload([scenario("A"), scenario("B")]))
    assert first["scenario_results"] == second["scenario_results"]


def test_duplicate_scenario_fails_closed():
    with pytest.raises(ValueError, match="SCENARIO_ID"):
        MODULE.build_report(payload([scenario("A"), scenario("A")]))


def test_empty_scenarios_fail_closed():
    with pytest.raises(ValueError, match="SCENARIOS"):
        MODULE.build_report(payload([]))


@pytest.mark.parametrize(
    "field", ["scenario_id", "baseline_latency_us", "optimized_latency_us", "behavior_equivalent"]
)
def test_missing_scenario_field_fails_closed(field):
    item = scenario()
    item.pop(field)
    with pytest.raises(ValueError, match="SCENARIO_FIELDS"):
        MODULE.build_report(payload([item]))


@pytest.mark.parametrize(
    ("field", "bad", "code"),
    [
        ("baseline_latency_us", 0, "BASELINE"),
        ("baseline_latency_us", True, "BASELINE"),
        ("optimized_latency_us", -1, "OPTIMIZED"),
        ("optimized_latency_us", True, "OPTIMIZED"),
        ("behavior_equivalent", 1, "BEHAVIOR"),
    ],
)
def test_invalid_scenario_measurement_fails_closed(field, bad, code):
    item = scenario()
    item[field] = bad
    with pytest.raises(ValueError, match=code):
        MODULE.build_report(payload([item]))


@pytest.mark.parametrize("bad", [-1, 10_001, True])
def test_invalid_minimum_improvement_fails_closed(bad):
    value = payload()
    value["minimum_aggregate_improvement_bps"] = bad
    _seal(value)
    with pytest.raises(ValueError, match="MINIMUM_IMPROVEMENT"):
        MODULE.build_report(value)


def test_bad_proof_hash_fails_closed():
    value = payload()
    value["proofs"]["scanner_artifact_hash"] = "bad"
    _seal(value)
    with pytest.raises(ValueError, match="SCANNER_HASH"):
        MODULE.build_report(value)


def test_input_tampering_fails_closed():
    value = payload()
    value["scenarios"][0]["optimized_latency_us"] = 799
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(value)


def test_atomic_publication(tmp_path):
    report = MODULE.build_report(payload())
    output = tmp_path / "performance.json"
    MODULE.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_report_never_enables_mutates_or_authorizes():
    report = MODULE.build_report(payload())
    assert report["wall_clock_used_as_correctness_gate"] is False
    assert report["paper_order_creation_enabled"] is False
    assert report["paper_order_creation_authorized"] is False
    assert report["paper_orders_created"] == 0
    assert report["production_database_mutated"] is False
    assert report["services_controlled"] is False
    assert report["exchange_requests_made"] is False
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_mutating_surface():
    source = SCRIPT.read_text()
    for token in (
        "import sqlite3",
        "import requests",
        "import subprocess",
        "systemctl ",
        "exchange_client.",
        "create_order(",
        "insert_order(",
        "/home/james",
    ):
        assert token not in source
