from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cl_snapshot_cache_integrity.py"
    spec = importlib.util.spec_from_file_location("phase4cl_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(module, ticker="KXTEST", sequence=1):
    identity = {"ticker": ticker, "sequence": sequence}
    value = {"yes_bids": [[55, 10]], "no_bids": [[44, 12]]}
    return {
        "namespace": "orderbook",
        "identity": identity,
        "cache_key": module.expected_cache_key("orderbook", identity),
        "value": value,
        "value_hash": module.canonical_hash(value),
    }


def _payload(module):
    payload = {"schema": module.INPUT_SCHEMA, "entries": [_entry(module)]}
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_valid_cache_manifest_passes_without_cache_io():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["status"] == "PASS"
    assert report["cache_usable"] is True
    assert report["invalid_count"] == 0
    assert report["cache_reads_performed"] == 0
    assert report["cache_writes_performed"] == 0


@pytest.mark.parametrize("kind", ["key", "value", "duplicate", "divergence"])
def test_integrity_failures_are_reported_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "key":
        payload["entries"][0]["cache_key"] = "a" * 64
    elif kind == "value":
        payload["entries"][0]["value_hash"] = "b" * 64
    elif kind == "duplicate":
        payload["entries"].append(dict(payload["entries"][0]))
    else:
        second = dict(payload["entries"][0])
        second["cache_key"] = "c" * 64
        payload["entries"].append(second)
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["status"] == "FAIL_CLOSED"
    assert report["cache_usable"] is False
    assert report["invalid_count"] >= 1


def test_identity_key_is_stable_across_mapping_order():
    module = _module()
    first = {"ticker": "KXTEST", "sequence": 1}
    second = {"sequence": 1, "ticker": "KXTEST"}
    assert module.expected_cache_key("orderbook", first) == module.expected_cache_key(
        "orderbook", second
    )


@pytest.mark.parametrize("kind", ["empty", "too_many", "fields", "namespace", "identity", "digest"])
def test_malformed_manifests_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["entries"] = []
    elif kind == "too_many":
        payload["entries"] = [payload["entries"][0]] * (module.MAX_ENTRIES + 1)
    elif kind == "fields":
        payload["entries"][0]["extra"] = True
    elif kind == "namespace":
        payload["entries"][0]["namespace"] = ""
    elif kind == "identity":
        payload["entries"][0]["identity"] = {}
    else:
        payload["entries"][0]["value_hash"] = "invalid"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_outer_tampering_and_unknown_fields_fail_closed():
    module = _module()
    payload = _payload(module)
    payload["entries"][0]["value"]["yes_bids"][0][0] = 56
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    payload = _payload(module)
    payload["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError, match="FIELDS"):
        module.build_report(payload)


def test_deterministic_atomic_publication(tmp_path: Path):
    module = _module()
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "cache-integrity.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_cache_connected_or_mutation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cl_snapshot_cache_integrity.py"
    ).read_text()
    for token in (
        "sqlite3",
        "redis",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
