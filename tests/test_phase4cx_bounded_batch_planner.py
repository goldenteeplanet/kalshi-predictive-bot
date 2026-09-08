from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cx_bounded_batch_planner.py"
    spec = importlib.util.spec_from_file_location("phase4cx_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(identifier, deadline=100, size=50, memory=50, duration=10, rate=1):
    return {
        "fixture_id": identifier,
        "estimated_bytes": size,
        "memory_bytes": memory,
        "estimated_ms": duration,
        "rate_units": rate,
        "deadline_ms": deadline,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "limits": {
            "max_batch_items": 2,
            "max_batch_bytes": 100,
            "max_batch_memory_bytes": 100,
            "rate_units": 4,
        },
        "fixtures": [
            _fixture("B", deadline=20),
            _fixture("A", deadline=20),
            _fixture("C", deadline=30),
            _fixture("D", deadline=30),
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_exact_bounds_pack_deterministically_by_deadline_and_id():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["status"] == "PLAN_COMPLETE"
    assert [batch["fixture_ids"] for batch in report["batches"]] == [["A", "B"], ["C", "D"]]
    assert all(batch["item_count"] == 2 for batch in report["batches"])
    assert all(batch["bytes"] == 100 for batch in report["batches"])
    assert all(batch["memory_bytes"] == 100 for batch in report["batches"])
    assert report["total_rate_units"] == 4
    assert report["refused"] == []
    assert report["execution_authorized"] is False


@pytest.mark.parametrize(
    ("field", "limit", "reason"),
    [
        ("estimated_bytes", 101, "ITEM_BYTES_EXCEED_BATCH_BOUND"),
        ("memory_bytes", 101, "ITEM_MEMORY_EXCEED_BATCH_BOUND"),
        ("rate_units", 5, "ITEM_RATE_EXCEED_WINDOW_BOUND"),
    ],
)
def test_single_item_overages_are_explicitly_refused(field: str, limit: int, reason: str):
    module = _module()
    payload = _payload(module)
    payload["fixtures"][0][field] = limit
    _rehash(module, payload)
    report = module.build_report(payload)
    refused = {row["fixture_id"]: row for row in report["refused"]}
    assert reason in refused["B"]["reasons"]


def test_rate_window_exhaustion_refuses_remaining_fixture():
    module = _module()
    payload = _payload(module)
    payload["limits"]["rate_units"] = 3
    _rehash(module, payload)
    report = module.build_report(payload)
    assert report["planned_fixture_count"] == 3
    assert report["refused"] == [{"fixture_id": "D", "reasons": ["RATE_WINDOW_EXHAUSTED"]}]


def test_projected_completion_deadline_refusal_is_explicit():
    module = _module()
    payload = _payload(module)
    payload["fixtures"][0]["deadline_ms"] = 10
    payload["fixtures"][1]["deadline_ms"] = 10
    payload["fixtures"][2]["deadline_ms"] = 15
    payload["fixtures"][2]["estimated_ms"] = 6
    _rehash(module, payload)
    report = module.build_report(payload)
    refused = {row["fixture_id"]: row for row in report["refused"]}
    assert refused["C"]["reasons"] == ["DEADLINE_MISSED"]


@pytest.mark.parametrize("kind", ["limits", "zero", "empty", "fields", "duplicate", "negative"])
def test_malformed_planner_inputs_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "limits":
        del payload["limits"]["rate_units"]
    elif kind == "zero":
        payload["limits"]["max_batch_items"] = 0
    elif kind == "empty":
        payload["fixtures"] = []
    elif kind == "fields":
        payload["fixtures"][0]["extra"] = True
    elif kind == "duplicate":
        payload["fixtures"][1]["fixture_id"] = "B"
    else:
        payload["fixtures"][0]["estimated_bytes"] = -1
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["limits"]["max_batch_items"] = 3
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "batch-plan.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cx_bounded_batch_planner.py"
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
