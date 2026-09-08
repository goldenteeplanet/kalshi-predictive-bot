from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ef_portfolio_snapshot_freshness_gate.py"
    spec = importlib.util.spec_from_file_location("phase4ef_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "as_of": "2026-08-26T12:00:00.000Z",
        "snapshot_version": "portfolio-v7",
        "max_age_ms": 1000,
        "max_skew_ms": 250,
        "fields": [
            {
                "field_id": "cash",
                "snapshot_version": "portfolio-v7",
                "observed_at": "2026-08-26T11:59:59.250Z",
                "value_hash": "a" * 64,
                "source_artifact_hash": "b" * 64,
            },
            {
                "field_id": "positions",
                "snapshot_version": "portfolio-v7",
                "observed_at": "2026-08-26T11:59:59.500Z",
                "value_hash": "c" * 64,
                "source_artifact_hash": "d" * 64,
            },
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_exact_age_and_skew_boundaries_pass():
    module = _module()
    payload = _payload(module)
    payload["fields"][0]["observed_at"] = "2026-08-26T11:59:59.000Z"
    payload["fields"][1]["observed_at"] = "2026-08-26T11:59:59.250Z"
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["status"] == "FRESH_COHERENT_SNAPSHOT"
    assert report["observed_skew_ms"] == 250
    assert report["risk_calculations_executed"] == 0


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("stale", "cash:STALE_FIELD"),
        ("future", "cash:FUTURE_OBSERVATION"),
        ("version", "cash:MIXED_SNAPSHOT_VERSION"),
        ("skew", "SNAPSHOT_OBSERVATION_SKEW_EXCEEDED"),
    ],
)
def test_freshness_or_coherence_failure_refuses(kind, reason):
    module = _module()
    payload = _payload(module)
    if kind == "stale":
        payload["fields"][0]["observed_at"] = "2026-08-26T11:59:58.999Z"
    elif kind == "future":
        payload["fields"][0]["observed_at"] = "2026-08-26T12:00:00.001Z"
    elif kind == "version":
        payload["fields"][0]["snapshot_version"] = "portfolio-v6"
    else:
        payload["fields"][0]["observed_at"] = "2026-08-26T11:59:59.249Z"
        payload["fields"][1]["observed_at"] = "2026-08-26T11:59:59.500Z"
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["status"] == "REFUSE"
    assert reason in report["reasons"]
    assert report["eligible_for_risk_evaluation"] is False


def test_multiple_reasons_are_deterministic_in_input_field_order():
    module = _module()
    payload = _payload(module)
    payload["fields"][0]["snapshot_version"] = "old"
    payload["fields"][0]["observed_at"] = "2026-08-26T11:59:58.000Z"
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["reasons"][:2] == ["cash:MIXED_SNAPSHOT_VERSION", "cash:STALE_FIELD"]


@pytest.mark.parametrize(
    "kind",
    ["empty", "duplicate", "timestamp", "offset", "hash", "age", "skew", "version", "fields"],
)
def test_malformed_evidence_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["fields"] = []
    elif kind == "duplicate":
        payload["fields"].append(copy.deepcopy(payload["fields"][0]))
    elif kind == "timestamp":
        payload["as_of"] = "not-time"
    elif kind == "offset":
        payload["fields"][0]["observed_at"] = "2026-08-26T06:00:00-06:00"
    elif kind == "hash":
        payload["fields"][0]["value_hash"] = "bad"
    elif kind == "age":
        payload["max_age_ms"] = True
    elif kind == "skew":
        payload["max_skew_ms"] = -1
    elif kind == "version":
        payload["snapshot_version"] = ""
    else:
        payload["fields"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_field_output_is_canonical_and_input_not_mutated():
    module = _module()
    payload = _payload(module)
    payload["fields"].reverse()
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    report = module.build_report(payload)
    assert [row["field_id"] for row in report["fields"]] == ["cash", "positions"]
    assert payload == original


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["max_age_ms"] = 999
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "freshness.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ef_portfolio_snapshot_freshness_gate.py"
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
