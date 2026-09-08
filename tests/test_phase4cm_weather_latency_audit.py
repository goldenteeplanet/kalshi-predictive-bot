from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cm_weather_latency_audit.py"
    spec = importlib.util.spec_from_file_location("phase4cm_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample(identifier, source, value, meaning="OBSERVATION_TIME", available=True):
    return {
        "sample_id": identifier,
        "reconciliation_key": "AUS-rain",
        "source": source,
        "available": available,
        "requested_at": "2026-08-26T00:00:00Z",
        "received_at": "2026-08-26T00:00:00.500000Z",
        "data_timestamp": "2026-08-25T23:59:00Z",
        "timestamp_meaning": meaning,
        "value": value,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "evaluated_at": "2026-08-26T00:00:00Z",
        "max_latency_ms": 500,
        "max_freshness_age_ms": 60_000,
        "reconciliation_tolerance": "0.2",
        "samples": [_sample("nws", "NOAA_NWS", "1.0"), _sample("other", "SECONDARY", "1.2")],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_boundary_latency_freshness_and_reconciliation_pass():
    module = _module()
    report = module.build_report(_payload(module))
    assert all(row["latency_ms"] == 500 for row in report["samples"])
    assert all(row["freshness_age_ms"] == 60_000 for row in report["samples"])
    assert all(row["latency_status"] == "PASS" for row in report["samples"])
    assert report["reconciliations"][0]["status"] == "PASS"
    assert report["network_calls_performed"] == 0


def test_slow_stale_and_divergent_are_distinct():
    module = _module()
    payload = _payload(module)
    payload["samples"][0]["received_at"] = "2026-08-26T00:00:00.501000Z"
    payload["samples"][0]["data_timestamp"] = "2026-08-25T23:58:59.999000Z"
    payload["samples"][1]["value"] = "1.201"
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["samples"][0]["latency_status"] == "SLOW"
    assert report["samples"][0]["freshness_status"] == "STALE"
    assert report["reconciliations"][0]["status"] == "DIVERGENT"


def test_incompatible_timestamp_meanings_are_not_reconciled():
    module = _module()
    payload = _payload(module)
    payload["samples"][1]["timestamp_meaning"] = "ISSUE_TIME"
    _rehash(module, payload)
    row = module.build_report(payload)["reconciliations"][0]
    assert row["status"] == "INCOMPATIBLE_TIMESTAMP_MEANING"
    assert row["spread"] is None


def test_unavailable_source_yields_insufficient_evidence():
    module = _module()
    payload = _payload(module)
    payload["samples"][1]["available"] = False
    _rehash(module, payload)
    assert module.build_report(payload)["reconciliations"][0]["status"] == (
        "INSUFFICIENT_AVAILABLE_SOURCES"
    )


@pytest.mark.parametrize(
    "kind", ["empty", "fields", "duplicate", "source", "availability", "meaning"]
)
def test_malformed_samples_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["samples"] = []
    elif kind == "fields":
        payload["samples"][0]["extra"] = True
    elif kind == "duplicate":
        payload["samples"][1]["sample_id"] = "nws"
    elif kind == "source":
        payload["samples"][0]["source"] = ""
    elif kind == "availability":
        payload["samples"][0]["available"] = 1
    else:
        payload["samples"][0]["timestamp_meaning"] = "UNKNOWN"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["negative_latency", "future_data", "bad_value", "bad_tolerance"])
def test_temporal_and_numeric_errors_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "negative_latency":
        payload["samples"][0]["received_at"] = "2026-08-25T23:59:59Z"
    elif kind == "future_data":
        payload["samples"][0]["data_timestamp"] = "2026-08-26T00:00:01Z"
    elif kind == "bad_value":
        payload["samples"][0]["value"] = "NaN"
    else:
        payload["reconciliation_tolerance"] = "NaN"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["samples"][0]["value"] = "9"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    output = tmp_path / "weather-audit.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_network_database_or_service_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cm_weather_latency_audit.py"
    ).read_text()
    for token in ("requests", "httpx", "sqlite3", "subprocess", "systemctl", "/home/james"):
        assert token not in source
