from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cf_concurrent_fetch_safety_model.py"
    spec = importlib.util.spec_from_file_location("phase4cf_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _simulation(module):
    simulation = {
        "schema": module.INPUT_SCHEMA,
        "max_concurrency": 2,
        "retry_limit": 2,
        "operations": [
            {"request_id": "third", "ordinal": 2, "outcomes": ["SUCCESS"], "result_hash": "3" * 64},
            {
                "request_id": "first",
                "ordinal": 0,
                "outcomes": ["THROTTLED", "SUCCESS"],
                "result_hash": "1" * 64,
            },
            {
                "request_id": "second",
                "ordinal": 1,
                "outcomes": ["TIMEOUT", "TIMEOUT", "TIMEOUT"],
                "result_hash": None,
            },
            {"request_id": "fourth", "ordinal": 3, "outcomes": ["CANCELLED"], "result_hash": None},
        ],
    }
    simulation["artifact_hash"] = module._hash(simulation)
    return simulation


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_model_is_deterministic_bounded_and_stably_merged():
    module = _module()
    report = module.build_model(_simulation(module))
    assert report == module.build_model(_simulation(module))
    assert [row["request_id"] for row in report["results"]] == [
        "first",
        "second",
        "third",
        "fourth",
    ]
    assert [row["status"] for row in report["results"]] == [
        "SUCCESS",
        "RETRY_EXHAUSTED",
        "SUCCESS",
        "CANCELLED",
    ]
    assert report["merged_success_hashes"] == ["1" * 64, "3" * 64]
    assert report["maximum_slot_used"] == 1
    assert report["execution_authorized"] is False


def test_malformed_is_not_retried_and_failure_is_isolated():
    module = _module()
    simulation = _simulation(module)
    simulation["operations"][0].update(outcomes=["MALFORMED", "SUCCESS"], result_hash=None)
    _rehash(module, simulation)
    report = module.build_model(simulation)
    row = next(row for row in report["results"] if row["request_id"] == "third")
    assert row["status"] == "MALFORMED"
    assert row["attempt_count"] == 1
    assert len(report["merged_success_hashes"]) == 1


@pytest.mark.parametrize(
    "kind",
    ("concurrency", "retries", "fields", "duplicate_id", "duplicate_ordinal", "outcome", "hash"),
)
def test_malformed_simulation_fails_closed(kind: str):
    module = _module()
    simulation = _simulation(module)
    if kind == "concurrency":
        simulation["max_concurrency"] = 0
    elif kind == "retries":
        simulation["retry_limit"] = True
    elif kind == "fields":
        simulation["operations"][0]["extra"] = True
    elif kind == "duplicate_id":
        simulation["operations"][1]["request_id"] = "third"
    elif kind == "duplicate_ordinal":
        simulation["operations"][1]["ordinal"] = 2
    elif kind == "outcome":
        simulation["operations"][0]["outcomes"] = ["UNKNOWN"]
    else:
        simulation["operations"][0]["result_hash"] = "bad"
    _rehash(module, simulation)
    with pytest.raises(ValueError):
        module.build_model(simulation)


def test_outer_tampering_fails_closed():
    module = _module()
    simulation = _simulation(module)
    simulation["extra"] = True
    with pytest.raises(ValueError, match="HASH"):
        module.build_model(simulation)


def test_atomic_publication_round_trip(tmp_path: Path):
    module = _module()
    report = module.build_model(_simulation(module))
    output = tmp_path / "model.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cf_concurrent_fetch_safety_model.py"
    ).read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "requests",
        "systemctl",
        "exchange_client",
        "/home/james",
    ):
        assert token not in source
