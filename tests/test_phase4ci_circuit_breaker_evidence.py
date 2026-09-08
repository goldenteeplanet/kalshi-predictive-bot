from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ci_circuit_breaker_evidence.py"
    spec = importlib.util.spec_from_file_location("phase4ci_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "api_open_threshold": 3,
        "quality_open_threshold": 2,
        "recovery_threshold": 2,
        "observations": [
            {
                "observed_at": "2026-08-26T00:00:00Z",
                "api_outcome": "TIMEOUT",
                "quality_outcome": "UNKNOWN",
            },
            {
                "observed_at": "2026-08-26T00:00:01Z",
                "api_outcome": "OK",
                "quality_outcome": "VALID",
            },
            {
                "observed_at": "2026-08-26T00:00:02Z",
                "api_outcome": "OK",
                "quality_outcome": "INVALID",
            },
            {
                "observed_at": "2026-08-26T00:00:03Z",
                "api_outcome": "OK",
                "quality_outcome": "INVALID",
            },
            {
                "observed_at": "2026-08-26T00:00:04Z",
                "api_outcome": "OK",
                "quality_outcome": "VALID",
            },
            {
                "observed_at": "2026-08-26T00:00:05Z",
                "api_outcome": "OK",
                "quality_outcome": "VALID",
            },
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_transient_api_and_persistent_quality_are_distinct():
    module = _module()
    report = module.build_report(_payload(module))
    states = [row["state"] for row in report["observations"]]
    assert states == [
        "DEGRADED_API",
        "RECOVERING",
        "RECOVERING",
        "OPEN_DATA_QUALITY",
        "RECOVERING",
        "CLOSED",
    ]
    assert report["final_state"] == "CLOSED"
    assert report["execution_authorized"] is False


def test_persistent_api_failure_opens_api_circuit_at_boundary():
    module = _module()
    payload = _payload(module)
    payload["observations"] = [
        {
            "observed_at": f"2026-08-26T00:00:0{i}Z",
            "api_outcome": "TIMEOUT",
            "quality_outcome": "UNKNOWN",
        }
        for i in range(3)
    ]
    _rehash(module, payload)
    rows = module.build_report(payload)["observations"]
    assert [row["state"] for row in rows] == ["DEGRADED_API", "DEGRADED_API", "OPEN_API"]


def test_unknown_quality_does_not_count_as_invalid():
    module = _module()
    payload = _payload(module)
    payload["observations"] = [
        {
            "observed_at": "2026-08-26T00:00:00Z",
            "api_outcome": "OK",
            "quality_outcome": "UNKNOWN",
        },
        {
            "observed_at": "2026-08-26T00:00:01Z",
            "api_outcome": "OK",
            "quality_outcome": "UNKNOWN",
        },
    ]
    _rehash(module, payload)
    assert module.build_report(payload)["final_state"] == "CLOSED"


@pytest.mark.parametrize(
    "field", ["api_open_threshold", "quality_open_threshold", "recovery_threshold"]
)
def test_invalid_thresholds_fail_closed(field: str):
    module = _module()
    payload = _payload(module)
    payload[field] = True
    _rehash(module, payload)
    with pytest.raises(ValueError, match="INVALID"):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["empty", "fields", "api", "quality", "timestamp", "order"])
def test_invalid_observations_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["observations"] = []
    elif kind == "fields":
        payload["observations"][0]["extra"] = True
    elif kind == "api":
        payload["observations"][0]["api_outcome"] = "BAD"
    elif kind == "quality":
        payload["observations"][0]["quality_outcome"] = "BAD"
    elif kind == "timestamp":
        payload["observations"][0]["observed_at"] = "2026-08-26"
    else:
        payload["observations"][1]["observed_at"] = payload["observations"][0]["observed_at"]
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_and_extra_fields_fail_closed():
    module = _module()
    payload = _payload(module)
    payload["api_open_threshold"] = 4
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    payload = _payload(module)
    payload["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError, match="FIELDS"):
        module.build_report(payload)


def test_deterministic_atomic_publication(tmp_path: Path):
    module = _module()
    first = module.build_report(_payload(module))
    assert first == module.build_report(_payload(module))
    output = tmp_path / "report.json"
    module.publish(output, first)
    assert json.loads(output.read_text()) == first
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_mutation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ci_circuit_breaker_evidence.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "/home/james",
        "time.sleep",
    ):
        assert token not in source
