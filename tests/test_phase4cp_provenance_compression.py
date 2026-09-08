from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cp_provenance_compression.py"
    spec = importlib.util.spec_from_file_location("phase4cp_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    provenance = {
        "source": "NOAA_NWS",
        "endpoint_contract": "observations-v1",
        "captured_at": "2026-08-26T00:00:00Z",
        "lineage": ["request", "response", "normalized"],
    }
    payload = {
        "schema": module.INPUT_SCHEMA,
        "records": [
            {"record_id": f"r-{index}", "payload": {"value": index}, "provenance": provenance}
            for index in range(20)
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_repeated_provenance_is_content_addressed_and_reconstructable():
    module = _module()
    payload = _payload(module)
    report = module.build_report(payload)
    assert report["unique_provenance_count"] == 1
    assert report["record_count"] == 20
    assert report["bytes_saved"] > 0
    assert module.reconstruct(report) == payload["records"]
    assert report["reconstruction_hash"] == module.canonical_hash(payload["records"])


def test_dictionary_and_output_are_deterministic():
    module = _module()
    first = module.build_report(_payload(module))
    assert first == module.build_report(_payload(module))
    assert list(first["provenance_dictionary"]) == sorted(first["provenance_dictionary"])


@pytest.mark.parametrize("kind", ["missing", "dictionary", "record", "duplicate"])
def test_tampered_compressed_reports_fail_closed(kind: str):
    module = _module()
    report = module.build_report(_payload(module))
    if kind == "missing":
        report["compressed_records"][0]["provenance_ref"] = "f" * 64
    elif kind == "dictionary":
        reference = next(iter(report["provenance_dictionary"]))
        report["provenance_dictionary"][reference]["source"] = "tampered"
    elif kind == "record":
        report["compressed_records"][0]["extra"] = True
    else:
        report["compressed_records"][1]["record_id"] = "r-0"
    report["artifact_hash"] = module._hash(report)
    with pytest.raises(ValueError):
        module.reconstruct(report)


@pytest.mark.parametrize("kind", ["empty", "too_many", "fields", "duplicate", "provenance"])
def test_malformed_inputs_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["records"] = []
    elif kind == "too_many":
        payload["records"] = [payload["records"][0]] * (module.MAX_RECORDS + 1)
    elif kind == "fields":
        payload["records"][0]["extra"] = True
    elif kind == "duplicate":
        payload["records"][1]["record_id"] = "r-0"
    else:
        payload["records"][0]["provenance"] = {}
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_outer_tampering_fails_closed():
    module = _module()
    payload = _payload(module)
    payload["records"][0]["payload"]["value"] = 99
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)


def test_atomic_publication(tmp_path: Path):
    module = _module()
    report = module.build_report(_payload(module))
    output = tmp_path / "compressed-provenance.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_network_service_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cp_provenance_compression.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "httpx",
        "subprocess",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
