from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ed_risk_cap_computation_optimization.py"
    spec = importlib.util.spec_from_file_location("phase4ed_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "cap_sets": [
            {
                "cap_set_id": "shared",
                "terms": [
                    {"term_id": "bankroll", "numerator": "10.00", "denominator": "0.30"},
                    {"term_id": "exposure", "numerator": "9", "denominator": "1"},
                ],
            }
        ],
        "candidates": [
            {"candidate_id": "b", "cap_set_id": "shared", "requested_quantity": 20},
            {"candidate_id": "a", "cap_set_id": "shared", "requested_quantity": 5},
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_exact_floor_cap_and_baseline_equivalence():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["cap_sets"][0]["effective_cap"] == 9
    assert [row["optimized_allowed_quantity"] for row in report["results"]] == [5, 9]
    assert all(
        row["legacy_allowed_quantity"] == row["optimized_allowed_quantity"]
        for row in report["results"]
    )
    assert report["status"] == "EXACT_CAP_EQUIVALENCE_PROVEN"


@pytest.mark.parametrize(
    "numerator,denominator,expected",
    [
        ("0", "0.1", 0),
        ("0.299999999999999999", "0.1", 2),
        ("0.300000000000000000", "0.1", 3),
        ("1E+3", "3", 333),
    ],
)
def test_decimal_boundaries_are_exact(numerator, denominator, expected):
    module = _module()
    payload = _payload(module)
    payload["cap_sets"][0]["terms"] = [
        {"term_id": "boundary", "numerator": numerator, "denominator": denominator}
    ]
    _rehash(module, payload)
    assert module.build_report(payload)["cap_sets"][0]["effective_cap"] == expected


def test_repeated_cap_set_is_computed_once_and_inputs_are_not_mutated():
    module = _module()
    payload = _payload(module)
    original = copy.deepcopy(payload)
    report = module.build_report(payload)
    assert report["legacy_work_units"] == 4
    assert report["optimized_work_units"] == 4
    assert payload == original


@pytest.mark.parametrize(
    "kind",
    [
        "empty_sets",
        "empty_candidates",
        "duplicate_set",
        "duplicate_term",
        "duplicate_candidate",
        "missing_set",
        "quantity",
        "zero_denominator",
        "negative",
        "nan",
        "fields",
    ],
)
def test_malformed_cap_evidence_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "empty_sets":
        payload["cap_sets"] = []
    elif kind == "empty_candidates":
        payload["candidates"] = []
    elif kind == "duplicate_set":
        payload["cap_sets"].append(copy.deepcopy(payload["cap_sets"][0]))
    elif kind == "duplicate_term":
        payload["cap_sets"][0]["terms"].append(dict(payload["cap_sets"][0]["terms"][0]))
    elif kind == "duplicate_candidate":
        payload["candidates"].append(dict(payload["candidates"][0]))
    elif kind == "missing_set":
        payload["candidates"][0]["cap_set_id"] = "missing"
    elif kind == "quantity":
        payload["candidates"][0]["requested_quantity"] = True
    elif kind == "zero_denominator":
        payload["cap_sets"][0]["terms"][0]["denominator"] = "0"
    elif kind == "negative":
        payload["cap_sets"][0]["terms"][0]["numerator"] = "-1"
    elif kind == "nan":
        payload["cap_sets"][0]["terms"][0]["numerator"] = "NaN"
    else:
        payload["cap_sets"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_input_order_is_canonicalized():
    module = _module()
    first = module.build_report(_payload(module))
    payload = _payload(module)
    payload["candidates"].reverse()
    payload["cap_sets"][0]["terms"].reverse()
    _rehash(module, payload)
    second = module.build_report(payload)
    assert first["results"] == second["results"]
    assert first["cap_sets"] == second["cap_sets"]


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["cap_sets"][0]["terms"][0]["numerator"] = "11"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "caps.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ed_risk_cap_computation_optimization.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_order",
        "/home/james",
    ):
        assert token not in source
