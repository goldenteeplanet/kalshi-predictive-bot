from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4db_feature_reuse_audit.py"
    spec = importlib.util.spec_from_file_location("phase4db_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instance(identifier, forecast, scope="IMMUTABLE", valid_until=None):
    return {
        "instance_id": identifier,
        "forecast_id": forecast,
        "feature_id": "weather_delta",
        "value": {"delta": 1.5},
        "computation_version": "v1",
        "upstream_hashes": {"weather": "a" * 64},
        "deterministic": True,
        "scope": scope,
        "valid_until": valid_until,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "evaluated_at": "2026-08-26T00:00:00Z",
        "instances": [
            _instance("i-1", "forecast-1"),
            _instance("i-2", "forecast-2"),
            _instance("i-3", "forecast-3", "TIME_BOUND", "2026-08-26T00:00:00Z"),
            _instance("i-4", "forecast-4", "FORECAST_SPECIFIC"),
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_identical_immutable_and_boundary_valid_features_form_reuse_group():
    module = _module()
    report = module.build_report(_payload(module))
    assert len(report["reuse_groups"]) == 1
    group = report["reuse_groups"][0]
    assert group["forecast_ids"] == ["forecast-1", "forecast-2", "forecast-3"]
    assert group["computations_saved"] == 2
    assert report["total_computations_saved"] == 2
    assert report["content_store_writes"] == 0
    by_id = {row["instance_id"]: row for row in report["decisions"]}
    assert by_id["i-3"]["eligible"] is True
    assert by_id["i-4"]["reasons"] == ["FORECAST_SPECIFIC_SCOPE"]


@pytest.mark.parametrize("field", ["value", "computation_version", "upstream_hashes"])
def test_content_identity_changes_prevent_cross_forecast_reuse(field: str):
    module = _module()
    payload = _payload(module)
    target = payload["instances"][1]
    if field == "value":
        target[field] = {"delta": 2.0}
    elif field == "computation_version":
        target[field] = "v2"
    else:
        target[field] = {"weather": "b" * 64}
    _rehash(module, payload)
    report = module.build_report(payload)
    by_id = {row["instance_id"]: row for row in report["decisions"]}
    assert by_id["i-1"]["content_key"] != by_id["i-2"]["content_key"]
    assert "forecast-2" not in report["reuse_groups"][0]["forecast_ids"]


def test_expired_and_nondeterministic_features_are_ineligible():
    module = _module()
    payload = _payload(module)
    payload["instances"][2]["valid_until"] = "2026-08-25T23:59:59.999000Z"
    payload["instances"][1]["deterministic"] = False
    _rehash(module, payload)
    by_id = {row["instance_id"]: row for row in module.build_report(payload)["decisions"]}
    assert by_id["i-3"]["reasons"] == ["FEATURE_EXPIRED"]
    assert by_id["i-2"]["reasons"] == ["NONDETERMINISTIC_COMPUTATION"]


def test_same_forecast_duplicates_do_not_prove_cross_forecast_reuse():
    module = _module()
    payload = _payload(module)
    payload["instances"] = [_instance("a", "same"), _instance("b", "same")]
    _rehash(module, payload)
    assert module.build_report(payload)["reuse_groups"] == []


@pytest.mark.parametrize("kind", ["empty", "fields", "duplicate", "identity", "upstream", "hash"])
def test_malformed_instances_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["instances"] = []
    elif kind == "fields":
        payload["instances"][0]["extra"] = True
    elif kind == "duplicate":
        payload["instances"][1]["instance_id"] = "i-1"
    elif kind == "identity":
        payload["instances"][0]["forecast_id"] = ""
    elif kind == "upstream":
        payload["instances"][0]["upstream_hashes"] = {}
    else:
        payload["instances"][0]["upstream_hashes"]["weather"] = "bad"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["scope", "deterministic", "immutable_expiry", "time_missing"])
def test_invalid_reuse_policy_fails_closed(kind: str):
    module = _module()
    payload = _payload(module)
    instance = payload["instances"][0]
    if kind == "scope":
        instance["scope"] = "UNKNOWN"
    elif kind == "deterministic":
        instance["deterministic"] = 1
    elif kind == "immutable_expiry":
        instance["valid_until"] = "2026-08-27T00:00:00Z"
    else:
        instance["scope"] = "TIME_BOUND"
        instance["valid_until"] = None
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["instances"][0]["computation_version"] = "tampered"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "reuse.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_forecast_cache_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4db_feature_reuse_audit.py"
    ).read_text()
    for token in (
        "sqlite3",
        "redis",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "create_forecast",
        "create_order",
        "/home/james",
    ):
        assert token not in source
