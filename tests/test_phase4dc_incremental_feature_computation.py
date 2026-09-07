import json
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

from scripts.local.phase4dc_incremental_feature_computation import (
    INPUT_SCHEMA,
    build_report,
    publish,
)


def signed(payload):
    payload.pop("artifact_hash", None)
    payload["artifact_hash"] = canonical_hash(payload)
    return payload


def fixture():
    nodes = [
        {"id": "a", "op": "SOURCE", "dependencies": [], "source_value": "2"},
        {"id": "b", "op": "SOURCE", "dependencies": [], "source_value": "3"},
        {"id": "c", "op": "ADD", "dependencies": ["a", "b"], "source_value": None},
        {"id": "d", "op": "MULTIPLY", "dependencies": ["c", "b"], "source_value": None},
        {"id": "independent", "op": "MAX", "dependencies": ["b"], "source_value": None},
    ]
    return signed(
        {
            "schema": INPUT_SCHEMA,
            "nodes": nodes,
            "previous_values": {"a": "2", "b": "3", "c": "5", "d": "15", "independent": "3"},
            "source_updates": {"a": "4"},
        }
    )


def test_incremental_matches_full_and_reuses_unaffected_nodes():
    report = build_report(fixture())
    assert report["full_equivalence"] is True
    assert report["values"] == {"a": "4", "b": "3", "c": "7", "d": "21", "independent": "3"}
    assert report["recomputed_node_ids"] == ["a", "c", "d"]
    assert report["reused_node_ids"] == ["b", "independent"]
    assert report["baseline_values_hash"] == report["incremental_values_hash"]


@pytest.mark.parametrize(
    "update, expected",
    [
        ({"a": "2"}, {"a", "c", "d"}),
        ({"b": "5"}, {"b", "c", "d", "independent"}),
        ({"a": "4", "b": "5"}, {"a", "b", "c", "d", "independent"}),
    ],
)
def test_changed_dependency_closure_is_exact(update, expected):
    payload = fixture()
    payload["source_updates"] = update
    signed(payload)
    assert set(build_report(payload)["recomputed_node_ids"]) == expected


def test_decimal_operations_and_canonical_rendering():
    payload = signed(
        {
            "schema": INPUT_SCHEMA,
            "nodes": [
                {"id": "x", "op": "SOURCE", "dependencies": [], "source_value": "10.00"},
                {"id": "y", "op": "SOURCE", "dependencies": [], "source_value": "4"},
                {"id": "sub", "op": "SUBTRACT", "dependencies": ["x", "y"], "source_value": None},
                {"id": "div", "op": "DIVIDE", "dependencies": ["x", "y"], "source_value": None},
                {"id": "min", "op": "MIN", "dependencies": ["x", "y"], "source_value": None},
            ],
            "previous_values": {"x": "10", "y": "4", "sub": "6", "div": "2.5", "min": "4"},
            "source_updates": {"x": "8.0"},
        }
    )
    assert build_report(payload)["values"] == {
        "x": "8",
        "y": "4",
        "sub": "4",
        "div": "2",
        "min": "4",
    }


def test_stale_or_tampered_previous_values_fail_closed():
    payload = fixture()
    payload["previous_values"]["c"] = "999"
    signed(payload)
    with pytest.raises(ValueError, match="PREVIOUS_BASELINE_MISMATCH"):
        build_report(payload)


@pytest.mark.parametrize(
    "mutation, error",
    [
        (lambda p: p["nodes"][2]["dependencies"].append("c"), "CYCLE"),
        (lambda p: p["nodes"][2]["dependencies"].append("missing"), "MISSING"),
        (lambda p: p["source_updates"].update({"c": "7"}), "UPDATE_TARGET"),
        (lambda p: p["nodes"][0].update({"source_value": "NaN"}), "DECIMAL"),
    ],
)
def test_invalid_graphs_fail_closed(mutation, error):
    payload = fixture()
    mutation(payload)
    signed(payload)
    with pytest.raises(ValueError, match=error):
        build_report(payload)


def test_zero_division_fails_closed():
    payload = fixture()
    payload["nodes"][2] = {
        "id": "c",
        "op": "DIVIDE",
        "dependencies": ["a", "b"],
        "source_value": None,
    }
    payload["previous_values"] = {
        "a": "2",
        "b": "3",
        "c": "0.6666666666666666666666666667",
        "d": "2",
        "independent": "3",
    }
    payload["source_updates"] = {"b": "0"}
    signed(payload)
    with pytest.raises(ValueError, match="COMPUTATION_INVALID"):
        build_report(payload)


def test_input_hash_tampering_fails_closed():
    payload = fixture()
    payload["source_updates"]["a"] = "9"
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        build_report(payload)


def test_deterministic_under_node_input_order():
    first = fixture()
    second = fixture()
    second["nodes"] = list(reversed(second["nodes"]))
    signed(second)
    left, right = build_report(first), build_report(second)
    assert left["values"] == right["values"]
    assert left["recomputed_node_ids"] == right["recomputed_node_ids"]


def test_atomic_publication(tmp_path):
    output = tmp_path / "nested" / "report.json"
    report = build_report(fixture())
    publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(output.parent.glob(".*"))


def test_report_is_non_executable():
    report = build_report(fixture())
    assert report["production_records_created"] == 0
    assert report["forecast_records_created"] == 0
    assert report["execution_authorized"] is False


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4dc_incremental_feature_computation.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_forecast",
        "create_order",
        "/home/james",
    ):
        assert token not in source
