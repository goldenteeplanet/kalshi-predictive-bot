from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cs_data_resource_bounds.py"
    spec = importlib.util.spec_from_file_location("phase4cs_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    limits = {
        "pages": 2,
        "markets": 10,
        "snapshots": 20,
        "bytes": 1000,
        "peak_memory_bytes": 5000,
        "elapsed_ms": 100,
    }
    payload = {
        "schema": module.INPUT_SCHEMA,
        "limits": limits,
        "stages": [
            {
                "name": "catalog",
                "usage": {
                    "pages": 1,
                    "markets": 5,
                    "snapshots": 0,
                    "bytes": 400,
                    "peak_memory_bytes": 3000,
                    "elapsed_ms": 40,
                },
            },
            {
                "name": "snapshots",
                "usage": {
                    "pages": 1,
                    "markets": 5,
                    "snapshots": 20,
                    "bytes": 600,
                    "peak_memory_bytes": 5000,
                    "elapsed_ms": 100,
                },
            },
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_exact_all_metric_boundaries_pass():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["status"] == "PASS"
    assert report["halt_stage"] is None
    final = report["stages"][-1]["cumulative"]
    assert final == report["limits"]
    assert report["execution_authorized"] is False


@pytest.mark.parametrize("metric", list(("pages", "markets", "snapshots", "bytes")))
def test_one_over_cumulative_counter_refuses_at_exact_stage(metric: str):
    module = _module()
    payload = _payload(module)
    payload["stages"][1]["usage"][metric] += 1
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["status"] == "REFUSE"
    assert report["halt_stage"] == "snapshots"
    assert metric in report["stages"][1]["exceeded_metrics"]


@pytest.mark.parametrize("metric", ["peak_memory_bytes", "elapsed_ms"])
def test_one_over_gauge_refuses(metric: str):
    module = _module()
    payload = _payload(module)
    payload["stages"][1]["usage"][metric] += 1
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["status"] == "REFUSE"
    assert report["halt_metric"] == metric


def test_later_stages_are_marked_not_evaluated_after_refusal():
    module = _module()
    payload = _payload(module)
    payload["stages"][0]["usage"]["pages"] = 3
    payload["stages"].append(
        {
            "name": "later",
            "usage": {
                "pages": 0,
                "markets": 0,
                "snapshots": 0,
                "bytes": 0,
                "peak_memory_bytes": 5000,
                "elapsed_ms": 100,
            },
        }
    )
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["stages"][0]["status"] == "REFUSE"
    assert report["stages"][1]["status"] == "NOT_EVALUATED_AFTER_REFUSAL"
    assert report["stages"][2]["status"] == "NOT_EVALUATED_AFTER_REFUSAL"


@pytest.mark.parametrize("kind", ["limits", "negative", "empty", "stage_fields", "duplicate"])
def test_malformed_inputs_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "limits":
        del payload["limits"]["bytes"]
    elif kind == "negative":
        payload["stages"][0]["usage"]["pages"] = -1
    elif kind == "empty":
        payload["stages"] = []
    elif kind == "stage_fields":
        payload["stages"][0]["extra"] = True
    else:
        payload["stages"][1]["name"] = "catalog"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize("metric", ["peak_memory_bytes", "elapsed_ms"])
def test_nonmonotonic_gauges_fail_closed(metric: str):
    module = _module()
    payload = _payload(module)
    payload["stages"][1]["usage"][metric] = payload["stages"][0]["usage"][metric] - 1
    _rehash(module, payload)
    with pytest.raises(ValueError, match="MONOTONIC"):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["limits"]["pages"] = 3
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "bounds.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_mutation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cs_data_resource_bounds.py"
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
