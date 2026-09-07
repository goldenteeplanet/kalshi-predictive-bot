from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = Path(__file__).parents[1] / "scripts/local/phase4eu_risk_paper_differential_replay.py"
SPEC = importlib.util.spec_from_file_location("phase4eu", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def decision(*, eligible=True, quantity=1, reasons=None):
    return {
        "eligible": eligible,
        "quantity": quantity,
        "caps": {
            "candidate_max_contracts": 1,
            "position_limit_contracts": 10,
            "loss_limit_micros": 500_000,
        },
        "reason_codes": [] if reasons is None else reasons,
    }


def case(case_id="A", legacy=None, optimized=None):
    baseline = decision()
    return {
        "case_id": case_id,
        "legacy": copy.deepcopy(baseline if legacy is None else legacy),
        "optimized": copy.deepcopy(baseline if optimized is None else optimized),
    }


def payload(cases=None):
    return _seal(
        {
            "schema": MODULE.INPUT_SCHEMA,
            "source_artifact_hash": "a" * 64,
            "cases": [case()] if cases is None else cases,
        }
    )


def test_identical_paths_certify_equivalence():
    report = MODULE.build_report(payload())
    assert report["all_equivalent"] is True
    assert report["paper_eligibility_equivalence_certified"] is True
    assert report["equivalent_count"] == report["case_count"] == 1
    assert report["gate_reason_codes"] == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda value: value.update(eligible=False, quantity=0), ["eligible", "quantity"]),
        (lambda value: value.update(quantity=2), ["quantity"]),
        (lambda value: value["caps"].update(candidate_max_contracts=2), ["caps"]),
        (lambda value: value.update(reason_codes=["CAP_APPLIED"]), ["reason_codes"]),
    ],
)
def test_each_differential_closes_equivalence_gate(mutate, expected):
    legacy = decision()
    optimized = decision()
    mutate(optimized)
    report = MODULE.build_report(payload([case(legacy=legacy, optimized=optimized)]))
    result = report["case_results"][0]
    assert result["equivalent"] is False
    assert result["difference_fields"] == expected
    assert result["reason_codes"] == [
        f"DIFFERENTIAL_{difference.upper()}_MISMATCH" for difference in expected
    ]
    assert report["all_equivalent"] is False
    assert report["paper_eligibility_equivalence_certified"] is False


def test_multiple_differences_follow_contract_order():
    optimized = decision(quantity=2, reasons=["OTHER"])
    optimized["caps"]["loss_limit_micros"] = 1
    report = MODULE.build_report(payload([case(optimized=optimized)]))
    assert report["case_results"][0]["difference_fields"] == ["quantity", "caps", "reason_codes"]


def test_reason_input_order_is_normalized_before_comparison():
    left = decision(reasons=["B", "A"])
    right = decision(reasons=["A", "B"])
    report = MODULE.build_report(payload([case(legacy=left, optimized=right)]))
    assert report["all_equivalent"] is True
    assert report["case_results"][0]["legacy"]["reason_codes"] == ["A", "B"]


def test_case_input_order_is_normalized():
    first = MODULE.build_report(payload([case("B"), case("A")]))
    second = MODULE.build_report(payload([case("A"), case("B")]))
    assert first["case_results"] == second["case_results"]


def test_ineligible_zero_quantity_paths_are_valid_and_equivalent():
    blocked = decision(eligible=False, quantity=0, reasons=["HARD_BLOCK"])
    report = MODULE.build_report(payload([case(legacy=blocked, optimized=blocked)]))
    assert report["all_equivalent"] is True


def test_duplicate_case_fails_closed():
    with pytest.raises(ValueError, match="DUPLICATE"):
        MODULE.build_report(payload([case("A"), case("A")]))


def test_empty_cases_fail_closed():
    with pytest.raises(ValueError, match="CASES"):
        MODULE.build_report(payload([]))


@pytest.mark.parametrize("field", ["case_id", "legacy", "optimized"])
def test_missing_case_field_fails_closed(field):
    value = case()
    value.pop(field)
    with pytest.raises(ValueError, match="CASE_FIELDS"):
        MODULE.build_report(payload([value]))


@pytest.mark.parametrize("path", ["legacy", "optimized"])
@pytest.mark.parametrize("field", ["eligible", "quantity", "caps", "reason_codes"])
def test_missing_decision_field_fails_closed(path, field):
    value = case()
    value[path].pop(field)
    with pytest.raises(ValueError, match=f"{path.upper()}_FIELDS"):
        MODULE.build_report(payload([value]))


@pytest.mark.parametrize("path", ["legacy", "optimized"])
def test_eligibility_quantity_inconsistency_fails_closed(path):
    value = case()
    value[path]["eligible"] = False
    with pytest.raises(ValueError, match="INCONSISTENT"):
        MODULE.build_report(payload([value]))


@pytest.mark.parametrize(
    ("field", "bad", "code"),
    [
        ("candidate_max_contracts", -1, "CANDIDATE_CAP"),
        ("position_limit_contracts", True, "POSITION_CAP"),
        ("loss_limit_micros", -1, "LOSS_CAP"),
    ],
)
def test_invalid_cap_fails_closed(field, bad, code):
    value = case()
    value["optimized"]["caps"][field] = bad
    with pytest.raises(ValueError, match=code):
        MODULE.build_report(payload([value]))


@pytest.mark.parametrize("reasons", [["A", "A"], [""], "A"])
def test_invalid_reason_codes_fail_closed(reasons):
    value = case()
    value["legacy"]["reason_codes"] = reasons
    with pytest.raises(ValueError, match="REASONS"):
        MODULE.build_report(payload([value]))


def test_bad_source_hash_fails_closed():
    value = payload()
    value["source_artifact_hash"] = "bad"
    _seal(value)
    with pytest.raises(ValueError, match="SOURCE_HASH"):
        MODULE.build_report(value)


def test_input_tampering_fails_closed():
    value = payload()
    value["cases"][0]["optimized"]["quantity"] = 2
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(value)


def test_atomic_publication(tmp_path):
    report = MODULE.build_report(payload())
    output = tmp_path / "replay.json"
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
