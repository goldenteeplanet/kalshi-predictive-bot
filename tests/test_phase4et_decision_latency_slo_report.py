from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = Path(__file__).parents[1] / "scripts/local/phase4et_decision_latency_slo_report.py"
SPEC = importlib.util.spec_from_file_location("phase4et", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def payload(*, elapsed=100, objective=100, end_objective=600):
    return _seal(
        {
            "schema": MODULE.INPUT_SCHEMA,
            "run_id": "synthetic-run-1",
            "source_artifact_hash": "a" * 64,
            "stages": [
                {"name": name, "elapsed_us": elapsed, "objective_us": objective}
                for name in MODULE.STAGES
            ],
            "end_to_end_objective_us": end_objective,
        }
    )


def test_exact_stage_and_end_boundaries_pass():
    report = MODULE.build_report(payload())
    assert report["all_slos_passed"] is True
    assert report["breach_reason_codes"] == []
    assert all(item["headroom_us"] == 0 and item["passed"] for item in report["stage_results"])
    assert report["end_to_end"] == {
        "elapsed_us": 600,
        "objective_us": 600,
        "headroom_us": 0,
        "passed": True,
        "reason_codes": [],
    }


@pytest.mark.parametrize("stage_name", MODULE.STAGES)
def test_each_stage_breach_has_deterministic_reason(stage_name):
    value = payload(end_objective=1000)
    next(item for item in value["stages"] if item["name"] == stage_name)["elapsed_us"] = 101
    _seal(value)
    report = MODULE.build_report(value)
    code = f"STAGE_{stage_name.upper()}_SLO_EXCEEDED"
    assert code in report["breach_reason_codes"]
    result = next(item for item in report["stage_results"] if item["name"] == stage_name)
    assert result["headroom_us"] == -1
    assert result["reason_codes"] == [code]


def test_end_to_end_can_breach_while_stages_pass():
    report = MODULE.build_report(payload(elapsed=90, objective=100, end_objective=539))
    assert all(item["passed"] for item in report["stage_results"])
    assert report["end_to_end"]["elapsed_us"] == 540
    assert report["end_to_end"]["reason_codes"] == ["END_TO_END_SLO_EXCEEDED"]
    assert report["breach_reason_codes"] == ["END_TO_END_SLO_EXCEEDED"]


def test_multiple_breaches_follow_canonical_stage_order_then_end_to_end():
    value = payload(end_objective=10)
    for item in value["stages"]:
        if item["name"] in {"features", "risk"}:
            item["elapsed_us"] = 101
    _seal(value)
    report = MODULE.build_report(value)
    assert report["breach_reason_codes"] == [
        "STAGE_FEATURES_SLO_EXCEEDED",
        "STAGE_RISK_SLO_EXCEEDED",
        "END_TO_END_SLO_EXCEEDED",
    ]


def test_zero_elapsed_is_valid():
    report = MODULE.build_report(payload(elapsed=0, objective=1, end_objective=1))
    assert report["end_to_end"]["elapsed_us"] == 0
    assert report["all_slos_passed"] is True


def test_stage_input_order_is_normalized():
    first = MODULE.build_report(payload())
    value = payload()
    value["stages"].reverse()
    _seal(value)
    second = MODULE.build_report(value)
    assert first["stage_results"] == second["stage_results"]
    assert first["breach_reason_codes"] == second["breach_reason_codes"]


def test_missing_stage_fails_closed():
    value = payload()
    value["stages"].pop()
    _seal(value)
    with pytest.raises(ValueError, match="STAGE_SET"):
        MODULE.build_report(value)


def test_duplicate_stage_fails_closed():
    value = payload()
    value["stages"][-1]["name"] = value["stages"][0]["name"]
    _seal(value)
    with pytest.raises(ValueError, match="STAGE_NAME"):
        MODULE.build_report(value)


def test_unknown_stage_fails_closed():
    value = payload()
    value["stages"][-1]["name"] = "execution"
    _seal(value)
    with pytest.raises(ValueError, match="STAGE_NAME"):
        MODULE.build_report(value)


@pytest.mark.parametrize("field", ["name", "elapsed_us", "objective_us"])
def test_missing_stage_field_fails_closed(field):
    value = payload()
    value["stages"][0].pop(field)
    _seal(value)
    with pytest.raises(ValueError, match="STAGE_FIELDS"):
        MODULE.build_report(value)


@pytest.mark.parametrize(
    ("field", "bad", "code"),
    [
        ("elapsed_us", -1, "ELAPSED"),
        ("elapsed_us", True, "ELAPSED"),
        ("objective_us", 0, "OBJECTIVE"),
        ("objective_us", True, "OBJECTIVE"),
    ],
)
def test_invalid_stage_measurement_fails_closed(field, bad, code):
    value = payload()
    value["stages"][0][field] = bad
    _seal(value)
    with pytest.raises(ValueError, match=code):
        MODULE.build_report(value)


@pytest.mark.parametrize("bad", [0, -1, True, 10**12 + 1])
def test_invalid_end_objective_fails_closed(bad):
    value = payload()
    value["end_to_end_objective_us"] = bad
    _seal(value)
    with pytest.raises(ValueError, match="END_OBJECTIVE"):
        MODULE.build_report(value)


def test_bad_source_hash_fails_closed():
    value = payload()
    value["source_artifact_hash"] = "bad"
    _seal(value)
    with pytest.raises(ValueError, match="SOURCE_HASH"):
        MODULE.build_report(value)


def test_input_tampering_fails_closed():
    value = payload()
    value["stages"][0]["elapsed_us"] = 99
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(value)


def test_atomic_publication(tmp_path):
    report = MODULE.build_report(payload())
    output = tmp_path / "slo.json"
    MODULE.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_report_never_authorizes_or_mutates():
    report = MODULE.build_report(payload())
    assert report["paper_eligibility_authorized"] is False
    assert report["paper_order_creation_authorized"] is False
    assert report["paper_orders_created"] == 0
    assert report["execution_authorized"] is False
    assert report["production_records_created"] == 0


def test_source_has_no_connected_or_mutating_surface():
    source = SCRIPT.read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "insert_order",
        "/home/james",
    ):
        assert token not in source
