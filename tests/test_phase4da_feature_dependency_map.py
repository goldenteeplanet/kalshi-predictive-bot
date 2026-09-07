from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4da_feature_dependency_map.py"
    spec = importlib.util.spec_from_file_location("phase4da_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "evidence": [
            {
                "evidence_id": "weather",
                "source": "NOAA_NWS",
                "schema_hash": "a" * 64,
                "artifact_hash": "b" * 64,
                "freshness_limit_ms": 60_000,
            },
            {
                "evidence_id": "market",
                "source": "KALSHI_READ_ONLY",
                "schema_hash": "c" * 64,
                "artifact_hash": "d" * 64,
                "freshness_limit_ms": 5_000,
            },
        ],
        "features": [
            {
                "feature_id": "weather_delta",
                "dependencies": [{"kind": "EVIDENCE", "id": "weather"}],
                "computation_cost_units": 10,
                "invalidation_triggers": list(module.TRIGGERS[:3]),
            },
            {
                "feature_id": "combined",
                "dependencies": [
                    {"kind": "FEATURE", "id": "weather_delta"},
                    {"kind": "EVIDENCE", "id": "market"},
                ],
                "computation_cost_units": 20,
                "invalidation_triggers": list(module.TRIGGERS),
            },
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_transitive_evidence_freshness_cost_and_order_are_resolved():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["topological_order"] == ["weather_delta", "combined"]
    by_id = {row["feature_id"]: row for row in report["features"]}
    combined = by_id["combined"]
    assert combined["upstream_evidence"] == ["market", "weather"]
    assert combined["feature_closure"] == ["weather_delta"]
    assert combined["effective_freshness_limit_ms"] == 5_000
    assert combined["cumulative_computation_cost_units"] == 30
    assert report["forecast_records_created"] == 0


def test_mapping_is_deterministic_across_input_order():
    module = _module()
    first = module.build_report(_payload(module))
    payload = _payload(module)
    payload["evidence"].reverse()
    payload["features"].reverse()
    _rehash(module, payload)
    second = module.build_report(payload)
    assert first["topological_order"] == second["topological_order"]
    assert first["features"] == second["features"]
    assert first["evidence"] == second["evidence"]


def test_cycle_fails_closed():
    module = _module()
    payload = _payload(module)
    payload["features"][0]["dependencies"] = [{"kind": "FEATURE", "id": "combined"}]
    payload["features"][0]["invalidation_triggers"] = list(module.TRIGGERS)
    _rehash(module, payload)
    with pytest.raises(ValueError, match="CYCLE"):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "fields", "kind", "empty"])
def test_invalid_dependencies_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    dependencies = payload["features"][0]["dependencies"]
    if kind == "missing":
        dependencies[0]["id"] = "missing"
    elif kind == "duplicate":
        dependencies.append(dict(dependencies[0]))
    elif kind == "fields":
        dependencies[0]["extra"] = True
    elif kind == "kind":
        dependencies[0]["kind"] = "UNKNOWN"
    else:
        payload["features"][0]["dependencies"] = []
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["base", "feature", "unknown", "duplicate"])
def test_incomplete_or_invalid_triggers_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "base":
        payload["features"][0]["invalidation_triggers"].remove("FRESHNESS_EXPIRED")
    elif kind == "feature":
        payload["features"][1]["invalidation_triggers"].remove("FEATURE_DEPENDENCY_CHANGED")
    elif kind == "unknown":
        payload["features"][0]["invalidation_triggers"].append("UNKNOWN")
    else:
        payload["features"][0]["invalidation_triggers"].append("SCHEMA_CHANGED")
    _rehash(module, payload)
    with pytest.raises(ValueError, match="TRIGGERS"):
        module.build_report(payload)


@pytest.mark.parametrize(
    "kind", ["nodes", "evidence_fields", "evidence_id", "hash", "freshness", "cost"]
)
def test_malformed_nodes_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "nodes":
        payload["evidence"] = []
    elif kind == "evidence_fields":
        payload["evidence"][0]["extra"] = True
    elif kind == "evidence_id":
        payload["evidence"][1]["evidence_id"] = "weather"
    elif kind == "hash":
        payload["evidence"][0]["schema_hash"] = "bad"
    elif kind == "freshness":
        payload["evidence"][0]["freshness_limit_ms"] = 0
    else:
        payload["features"][0]["computation_cost_units"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["features"][0]["computation_cost_units"] = 11
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "feature-map.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_forecast_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4da_feature_dependency_map.py"
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
