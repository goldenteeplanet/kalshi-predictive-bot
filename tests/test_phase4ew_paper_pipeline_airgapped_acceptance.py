from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = Path(__file__).parents[1] / "scripts/local/phase4ew_paper_pipeline_airgapped_acceptance.py"
SPEC = importlib.util.spec_from_file_location("phase4ew", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def scenario(scenario_id):
    gates = dict.fromkeys(MODULE.GATES, True)
    if scenario_id != "SUCCESS":
        gate = next(name for name, code in MODULE.REFUSAL_CODES.items() if code == scenario_id)
        gates[gate] = False
    actual = MODULE._evaluate(gates)
    value = {
        "scenario_id": scenario_id,
        "gates": gates,
        "expected": {"outcome": actual["outcome"], "reason_codes": actual["reason_codes"]},
    }
    value["fixture_hash"] = canonical_hash(value)
    return value


def payload():
    return _seal(
        {
            "schema": MODULE.INPUT_SCHEMA,
            "air_gap": {
                "network_enabled": False,
                "services_available": False,
                "credentials_present": False,
                "order_creation_enabled": False,
                "database_mode": "DISPOSABLE_SYNTHETIC",
                "database_identity_hash": "a" * 64,
            },
            "scenarios": [scenario(scenario_id) for scenario_id in MODULE.REQUIRED_SCENARIOS],
        }
    )


def _reseal_scenario(value):
    value["fixture_hash"] = canonical_hash(
        {key: item for key, item in value.items() if key != "fixture_hash"}
    )


def test_success_and_every_refusal_class_pass_acceptance():
    report = MODULE.build_report(payload())
    assert report["required_scenarios"] == list(MODULE.REQUIRED_SCENARIOS)
    assert report["scenario_count"] == len(MODULE.GATES) + 1
    assert report["all_refusal_classes_covered"] is True
    assert report["acceptance_passed"] is True
    assert all(result["expected_matched"] for result in report["scenario_results"])


def test_success_reaches_but_does_not_cross_creation_boundary():
    report = MODULE.build_report(payload())
    success = report["scenario_results"][0]["actual"]
    assert success["outcome"] == "CREATION_BOUNDARY_REACHED_NOT_CROSSED"
    assert success["creation_boundary_reached"] is True
    assert report["paper_order_creation_boundary_crossed"] is False


@pytest.mark.parametrize("gate", MODULE.GATES)
def test_each_refusal_stops_at_exact_gate(gate):
    report = MODULE.build_report(payload())
    code = MODULE.REFUSAL_CODES[gate]
    result = next(item for item in report["scenario_results"] if item["scenario_id"] == code)
    assert result["actual"]["stopped_at_gate"] == gate
    assert result["actual"]["reason_codes"] == [code]
    assert result["actual"]["creation_boundary_reached"] is False


@pytest.mark.parametrize(
    "field",
    ["network_enabled", "services_available", "credentials_present", "order_creation_enabled"],
)
def test_any_connected_capability_fails_closed(field):
    value = payload()
    value["air_gap"][field] = True
    _seal(value)
    with pytest.raises(ValueError, match="AIR_GAP_NOT_ENFORCED"):
        MODULE.build_report(value)


def test_non_disposable_database_mode_fails_closed():
    value = payload()
    value["air_gap"]["database_mode"] = "PRODUCTION"
    _seal(value)
    with pytest.raises(ValueError, match="DATABASE_MODE"):
        MODULE.build_report(value)


def test_missing_refusal_scenario_fails_closed():
    value = payload()
    value["scenarios"].pop()
    _seal(value)
    with pytest.raises(ValueError, match="SCENARIO_SET"):
        MODULE.build_report(value)


def test_duplicate_scenario_fails_closed():
    value = payload()
    value["scenarios"][-1] = value["scenarios"][0]
    _seal(value)
    with pytest.raises(ValueError, match="SCENARIO_ID"):
        MODULE.build_report(value)


def test_success_with_false_gate_fails_closed():
    value = payload()
    value["scenarios"][0]["gates"][MODULE.GATES[0]] = False
    _reseal_scenario(value["scenarios"][0])
    _seal(value)
    with pytest.raises(ValueError, match="SUCCESS_FIXTURE"):
        MODULE.build_report(value)


def test_refusal_fixture_must_isolate_one_gate():
    value = payload()
    refusal = value["scenarios"][1]
    refusal["gates"][MODULE.GATES[1]] = False
    _reseal_scenario(refusal)
    _seal(value)
    with pytest.raises(ValueError, match="NOT_ISOLATED"):
        MODULE.build_report(value)


def test_expected_result_mismatch_closes_acceptance_without_authorizing():
    value = payload()
    value["scenarios"][0]["expected"]["outcome"] = "REFUSED"
    _reseal_scenario(value["scenarios"][0])
    _seal(value)
    report = MODULE.build_report(value)
    assert report["acceptance_passed"] is False
    assert report["scenario_results"][0]["acceptance_reason_codes"] == ["EXPECTED_OUTCOME_MISMATCH"]
    assert report["paper_order_creation_authorized"] is False


def test_fixture_tampering_fails_closed():
    value = payload()
    value["scenarios"][0]["expected"]["outcome"] = "REFUSED"
    _seal(value)
    with pytest.raises(ValueError, match="FIXTURE_HASH"):
        MODULE.build_report(value)


def test_outer_input_tampering_fails_closed():
    value = payload()
    value["air_gap"]["database_identity_hash"] = "b" * 64
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(value)


def test_scenario_input_order_is_normalized():
    first = MODULE.build_report(payload())
    value = payload()
    value["scenarios"].reverse()
    _seal(value)
    second = MODULE.build_report(value)
    assert first["scenario_results"] == second["scenario_results"]


def test_disposable_database_remains_byte_identical(tmp_path):
    database = tmp_path / "synthetic.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO fixture(value) VALUES ('synthetic')")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    MODULE.build_report(payload())
    after = hashlib.sha256(database.read_bytes()).hexdigest()
    assert before == after


def test_atomic_publication(tmp_path):
    report = MODULE.build_report(payload())
    output = tmp_path / "acceptance.json"
    MODULE.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_report_never_connects_mutates_or_authorizes():
    report = MODULE.build_report(payload())
    assert report["production_database_mutated"] is False
    assert report["services_controlled"] is False
    assert report["exchange_requests_made"] is False
    assert report["paper_order_creation_authorized"] is False
    assert report["paper_orders_created"] == 0
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
