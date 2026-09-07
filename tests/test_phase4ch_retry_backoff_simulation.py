from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ch_retry_backoff_simulation.py"
    spec = importlib.util.spec_from_file_location("phase4ch_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "retry_limit": 2,
        "base_backoff_ms": 100,
        "scenarios": [
            {
                "name": "throttle",
                "outcomes": ["THROTTLED", "SUCCESS"],
                "retry_after_ms": [250, None],
                "partial_page_idempotent": True,
            },
            {
                "name": "timeout",
                "outcomes": ["TIMEOUT", "TIMEOUT", "TIMEOUT"],
                "retry_after_ms": [None, None, None],
                "partial_page_idempotent": True,
            },
            {
                "name": "partial",
                "outcomes": ["PARTIAL_PAGE", "SUCCESS"],
                "retry_after_ms": [None, None],
                "partial_page_idempotent": True,
            },
            {
                "name": "malformed",
                "outcomes": ["MALFORMED", "SUCCESS"],
                "retry_after_ms": [None, None],
                "partial_page_idempotent": True,
            },
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_simulation_is_deterministic_and_covers_retry_modes():
    module = _module()
    report = module.build_simulation(_payload(module))
    assert report == module.build_simulation(_payload(module))
    by_name = {row["name"]: row for row in report["scenarios"]}
    assert by_name["throttle"]["status"] == "SUCCESS"
    assert by_name["throttle"]["attempts"][0]["backoff_before_next_ms"] == 250
    assert by_name["timeout"]["status"] == "RETRY_EXHAUSTED"
    assert [row["backoff_before_next_ms"] for row in by_name["timeout"]["attempts"]] == [
        100,
        200,
        0,
    ]
    assert by_name["partial"]["status"] == "SUCCESS"
    assert by_name["malformed"]["attempt_count"] == 1
    assert report["sleep_calls"] == 0
    assert report["execution_authorized"] is False


def test_non_idempotent_partial_page_is_terminal():
    module = _module()
    payload = _payload(module)
    payload["scenarios"][2]["partial_page_idempotent"] = False
    _rehash(module, payload)
    row = module.build_simulation(payload)["scenarios"][2]
    assert row["status"] == "UNSAFE_PARTIAL_PAGE"
    assert row["attempt_count"] == 1


def test_backoff_is_capped():
    module = _module()
    payload = _payload(module)
    payload["base_backoff_ms"] = module.MAX_BACKOFF_MS
    _rehash(module, payload)
    row = module.build_simulation(payload)["scenarios"][1]
    assert [attempt["backoff_before_next_ms"] for attempt in row["attempts"]] == [
        module.MAX_BACKOFF_MS,
        module.MAX_BACKOFF_MS,
        0,
    ]


@pytest.mark.parametrize(
    "kind", ("retry", "base", "fields", "duplicate", "outcome", "shape", "value", "idempotent")
)
def test_malformed_scenarios_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "retry":
        payload["retry_limit"] = True
    elif kind == "base":
        payload["base_backoff_ms"] = 0
    elif kind == "fields":
        payload["scenarios"][0]["extra"] = True
    elif kind == "duplicate":
        payload["scenarios"][1]["name"] = "throttle"
    elif kind == "outcome":
        payload["scenarios"][0]["outcomes"] = ["UNKNOWN"]
    elif kind == "shape":
        payload["scenarios"][0]["retry_after_ms"] = []
    elif kind == "value":
        payload["scenarios"][0]["retry_after_ms"][0] = -1
    else:
        payload["scenarios"][0]["partial_page_idempotent"] = "yes"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_simulation(payload)


def test_outer_tampering_fails_closed():
    module = _module()
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH"):
        module.build_simulation(payload)


def test_atomic_publication_round_trip(tmp_path: Path):
    module = _module()
    report = module.build_simulation(_payload(module))
    output = tmp_path / "simulation.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ch_retry_backoff_simulation.py"
    ).read_text()
    for token in (
        "sqlite3",
        "subprocess",
        "requests",
        "systemctl",
        "exchange_client",
        "/home/james",
        "time.sleep",
    ):
        assert token not in source
