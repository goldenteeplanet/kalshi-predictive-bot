from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ee_portfolio_snapshot_minimality.py"
    spec = importlib.util.spec_from_file_location("phase4ee_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _field(identifier, digit):
    return {
        "field_id": identifier,
        "value_hash": digit * 64,
        "source_artifact_hash": digit.upper() * 64,
        "immutable": True,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "fields": [
            _field("cash", "a"),
            _field("positions", "b"),
            _field("derived_exposure", "c"),
            _field("unused", "d"),
        ],
        "requirements": [
            {
                "requirement_id": "sizing_cash",
                "decision": "POSITION_SIZING",
                "acceptable_fields": ["cash"],
            },
            {
                "requirement_id": "sizing_exposure",
                "decision": "POSITION_SIZING",
                "acceptable_fields": ["positions", "derived_exposure"],
            },
            {
                "requirement_id": "risk_exposure",
                "decision": "ADVANCED_RISK",
                "acceptable_fields": ["positions", "derived_exposure"],
            },
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_minimum_sufficient_snapshot_and_lexicographic_tie_break():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["selected_field_ids"] == ["cash", "derived_exposure"]
    assert report["minimum_field_count"] == 2
    assert report["omitted_field_ids"] == ["positions", "unused"]
    assert all(row["selected_fields"] for row in report["coverage"])
    assert report["portfolio_reads_per_evaluation"] == 0


def test_overlapping_requirements_reduce_snapshot_size():
    module = _module()
    payload = _payload(module)
    payload["requirements"][0]["acceptable_fields"].append("derived_exposure")
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["selected_field_ids"] == ["derived_exposure"]


def test_input_order_is_canonicalized_and_not_mutated():
    module = _module()
    first = module.build_report(_payload(module))
    payload = _payload(module)
    payload["fields"].reverse()
    payload["requirements"].reverse()
    for row in payload["requirements"]:
        row["acceptable_fields"].reverse()
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    second = module.build_report(payload)
    assert first["selected_field_ids"] == second["selected_field_ids"]
    assert first["coverage"] == second["coverage"]
    assert payload == original


@pytest.mark.parametrize(
    "kind",
    [
        "empty_fields",
        "too_many",
        "empty_requirements",
        "duplicate_field",
        "duplicate_requirement",
        "missing_field",
        "duplicate_alternative",
        "unknown_decision",
        "missing_decision",
        "mutable",
        "hash",
        "fields",
    ],
)
def test_malformed_or_insufficient_evidence_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "empty_fields":
        payload["fields"] = []
    elif kind == "too_many":
        payload["fields"] = [_field(f"field-{i}", "a") for i in range(module.MAX_FIELDS + 1)]
    elif kind == "empty_requirements":
        payload["requirements"] = []
    elif kind == "duplicate_field":
        payload["fields"].append(dict(payload["fields"][0]))
    elif kind == "duplicate_requirement":
        payload["requirements"].append(copy.deepcopy(payload["requirements"][0]))
    elif kind == "missing_field":
        payload["requirements"][0]["acceptable_fields"] = ["missing"]
    elif kind == "duplicate_alternative":
        payload["requirements"][1]["acceptable_fields"].append("positions")
    elif kind == "unknown_decision":
        payload["requirements"][0]["decision"] = "UNKNOWN"
    elif kind == "missing_decision":
        payload["requirements"] = [
            row for row in payload["requirements"] if row["decision"] == "POSITION_SIZING"
        ]
    elif kind == "mutable":
        payload["fields"][0]["immutable"] = False
    elif kind == "hash":
        payload["fields"][0]["value_hash"] = "bad"
    else:
        payload["fields"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["requirements"][0]["acceptable_fields"].append("unused")
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "snapshot.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ee_portfolio_snapshot_minimality.py"
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
