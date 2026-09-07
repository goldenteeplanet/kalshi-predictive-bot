from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cy_partial_failure_isolation.py"
    spec = importlib.util.spec_from_file_location("phase4cy_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _data(identifier, source, market, page=1):
    return {
        "event_id": identifier,
        "source": source,
        "page": page,
        "market": market,
        "scope": "DATA",
        "status": "SUCCESS",
        "payload": {"value": identifier},
    }


def _failure(identifier, source, market, scope, page=0):
    return {
        "event_id": identifier,
        "source": source,
        "page": page,
        "market": market,
        "scope": scope,
        "status": "FAILED",
        "payload": None,
    }


def _payload(module, failures=None):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "events": [
            _data("a-1", "source-a", "market-1"),
            _data("a-2", "source-a", "market-2"),
            _data("b-1", "source-b", "market-1"),
            *(failures or []),
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def _artifact_hashes(report):
    return {(row["source"], row["market"]): row["artifact_hash"] for row in report["artifacts"]}


@pytest.mark.parametrize(
    "failure",
    [
        _failure("fail-market", "source-a", "market-1", "MARKET"),
        _failure("fail-page", "source-a", "market-1", "PAGE", page=1),
    ],
)
def test_market_or_page_failure_does_not_change_unrelated_artifact_hashes(failure):
    module = _module()
    baseline = _artifact_hashes(module.build_report(_payload(module)))
    report = module.build_report(_payload(module, [failure]))
    actual = _artifact_hashes(report)
    assert ("source-a", "market-1") not in actual
    assert actual[("source-a", "market-2")] == baseline[("source-a", "market-2")]
    assert actual[("source-b", "market-1")] == baseline[("source-b", "market-1")]
    assert report["partial_artifacts_published"] == 0


def test_source_failure_quarantines_only_that_source():
    module = _module()
    baseline = _artifact_hashes(module.build_report(_payload(module)))
    failure = _failure("fail-source", "source-a", "all", "SOURCE")
    report = module.build_report(_payload(module, [failure]))
    actual = _artifact_hashes(report)
    assert all(source != "source-a" for source, _market in actual)
    assert actual[("source-b", "market-1")] == baseline[("source-b", "market-1")]
    assert len(report["quarantined"]) == 2


def test_successful_artifact_record_order_is_deterministic():
    module = _module()
    payload = _payload(module)
    payload["events"].insert(0, _data("a-0", "source-a", "market-1", page=0))
    _rehash(module, payload)
    report = module.build_report(payload)
    artifact = next(
        row
        for row in report["artifacts"]
        if row["source"] == "source-a" and row["market"] == "market-1"
    )
    assert [row["event_id"] for row in artifact["records"]] == ["a-0", "a-1"]
    assert artifact["artifact_hash"] == module._hash(artifact)


@pytest.mark.parametrize("kind", ["empty", "fields", "duplicate", "source", "page", "scope"])
def test_malformed_events_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["events"] = []
    elif kind == "fields":
        payload["events"][0]["extra"] = True
    elif kind == "duplicate":
        payload["events"][1]["event_id"] = "a-1"
    elif kind == "source":
        payload["events"][0]["source"] = ""
    elif kind == "page":
        payload["events"][0]["page"] = True
    else:
        payload["events"][0]["scope"] = "UNKNOWN"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize(
    "kind", ["data_status", "data_payload", "failure_status", "failure_payload"]
)
def test_scope_status_payload_contracts_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind.startswith("failure"):
        payload["events"].append(_failure("failure", "source-a", "market-1", "MARKET"))
        event = payload["events"][-1]
        event["status" if kind == "failure_status" else "payload"] = (
            "SUCCESS" if kind == "failure_status" else {}
        )
    else:
        event = payload["events"][0]
        event["status" if kind == "data_status" else "payload"] = (
            "FAILED" if kind == "data_status" else None
        )
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["events"][0]["payload"]["value"] = "tampered"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "isolation.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_production_publication_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cy_partial_failure_isolation.py"
    ).read_text()
    for token in (
        "sqlite3",
        "requests",
        "subprocess",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
