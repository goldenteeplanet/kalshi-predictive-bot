import json
from copy import deepcopy
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4dw_performance_regression_gate import INPUT_SCHEMA, build_report, publish

H1, H2 = "1" * 64, "2" * 64


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def fixture(current=None):
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "baseline": {
                "work_units": 1000,
                "allocation_units": 500,
                "parse_units": 100,
                "logical_output_hash": H1,
            },
            "current": current
            or {
                "work_units": 1050,
                "allocation_units": 500,
                "parse_units": 100,
                "logical_output_hash": H1,
            },
            "thresholds": {
                "work_units": {
                    "max_absolute_increase": 50,
                    "max_percent_increase_basis_points": 500,
                },
                "allocation_units": {
                    "max_absolute_increase": 0,
                    "max_percent_increase_basis_points": 0,
                },
                "parse_units": {"max_absolute_increase": 0, "max_percent_increase_basis_points": 0},
            },
        }
    )


def test_exact_absolute_and_percent_boundaries_pass():
    report = build_report(fixture())
    assert report["status"] == "PASS"
    assert report["wall_clock_used_as_gate"] is False


def test_one_unit_beyond_absolute_boundary_fails():
    current = {
        "work_units": 1051,
        "allocation_units": 500,
        "parse_units": 100,
        "logical_output_hash": H1,
    }
    report = build_report(fixture(current))
    assert report["status"] == "FAIL"
    assert "WORK_UNITS_REGRESSION" in report["failures"]


def test_percent_threshold_uses_exact_cross_multiplication():
    payload = fixture(
        {"work_units": 1050, "allocation_units": 500, "parse_units": 100, "logical_output_hash": H1}
    )
    payload["thresholds"]["work_units"]["max_absolute_increase"] = 999
    payload["thresholds"]["work_units"]["max_percent_increase_basis_points"] = 499
    signed(payload)
    assert "WORK_UNITS_REGRESSION" in build_report(payload)["failures"]


def test_reductions_always_have_zero_increase():
    current = {"work_units": 1, "allocation_units": 1, "parse_units": 1, "logical_output_hash": H1}
    assert build_report(fixture(current))["status"] == "PASS"


def test_zero_baseline_allows_no_percent_increase():
    payload = fixture()
    payload["baseline"]["parse_units"] = 0
    payload["current"]["parse_units"] = 1
    payload["thresholds"]["parse_units"]["max_absolute_increase"] = 1
    payload["thresholds"]["parse_units"]["max_percent_increase_basis_points"] = 10000
    signed(payload)
    result = next(
        row for row in build_report(payload)["metric_results"] if row["metric"] == "parse_units"
    )
    assert result["absolute_pass"] is True
    assert result["percent_pass"] is False


def test_logical_output_change_fails_even_when_metrics_improve():
    current = {"work_units": 1, "allocation_units": 1, "parse_units": 1, "logical_output_hash": H2}
    report = build_report(fixture(current))
    assert report["status"] == "FAIL"
    assert report["failures"] == ["LOGICAL_OUTPUT_CHANGED"]


@pytest.mark.parametrize(
    "field,value", [("work_units", -1), ("allocation_units", True), ("parse_units", 10**15 + 1)]
)
def test_invalid_metric_fails_closed(field, value):
    payload = fixture()
    payload["current"][field] = value
    signed(payload)
    with pytest.raises(ValueError, match="METRIC_INVALID"):
        build_report(payload)


def test_invalid_threshold_fails_closed():
    payload = fixture()
    payload["thresholds"]["work_units"]["max_percent_increase_basis_points"] = 1_000_001
    signed(payload)
    with pytest.raises(ValueError, match="THRESHOLD_INVALID"):
        build_report(payload)


def test_tampering_fails_closed():
    payload = fixture()
    payload["current"]["work_units"] = 1
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        build_report(payload)


def test_deterministic_and_nonmutating():
    payload = fixture()
    before = deepcopy(payload)
    assert build_report(payload) == build_report(payload)
    assert payload == before


def test_atomic_publication(tmp_path):
    output = tmp_path / "nested" / "report.json"
    report = build_report(fixture())
    publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(output.parent.glob(".*"))


def test_non_executable():
    report = build_report(fixture())
    assert report["production_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_wall_clock_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dw_performance_regression_gate.py"
    ).read_text()
    for token in (
        "sqlite3",
        "import requests",
        "subprocess",
        "perf_counter",
        "sleep(",
        "systemctl",
        "create_order",
        "/home/james",
    ):
        assert token not in source
