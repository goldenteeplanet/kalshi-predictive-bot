from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = (
        Path(__file__).parents[1] / "scripts/local/phase4ei_deterministic_candidate_scheduling.py"
    )
    spec = importlib.util.spec_from_file_location("phase4ei_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(
    identifier,
    deadline="2026-08-26T12:00:10.000Z",
    observed="2026-08-26T11:59:59.500Z",
    freshness=1000,
):
    return {
        "candidate_id": identifier,
        "deadline": deadline,
        "evidence_observed_at": observed,
        "freshness_limit_ms": freshness,
        "work_units": 3,
        "evidence_hash": "a" * 64,
    }


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "as_of": "2026-08-26T12:00:00.000Z",
        "candidates": [
            _candidate("later", "2026-08-26T12:00:20.000Z"),
            _candidate("urgent", "2026-08-26T12:00:05.000Z"),
            _candidate("freshness", "2026-08-26T12:00:20.000Z", freshness=500),
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_order_is_deadline_then_freshness_then_identity():
    module = _module()
    report = module.build_report(_payload(module))
    assert [row["candidate_id"] for row in report["schedule"]] == ["urgent", "freshness", "later"]
    assert [row["schedule_position"] for row in report["schedule"]] == [1, 2, 3]
    assert report["evaluations_executed"] == 0


def test_identity_breaks_exact_tie():
    module = _module()
    payload = _payload(module)
    payload["candidates"] = [_candidate("b"), _candidate("a")]
    _rehash(module, payload)
    assert [row["candidate_id"] for row in module.build_report(payload)["schedule"]] == ["a", "b"]


def test_exact_deadline_and_freshness_boundaries_schedule():
    module = _module()
    payload = _payload(module)
    payload["candidates"] = [
        _candidate(
            "edge", deadline=payload["as_of"], observed="2026-08-26T11:59:59.000Z", freshness=1000
        )
    ]
    _rehash(module, payload)
    row = module.build_report(payload)["schedule"][0]
    assert row["deadline_slack_ms"] == 0
    assert row["freshness_remaining_ms"] == 0


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("deadline", "DEADLINE_EXPIRED"),
        ("stale", "EVIDENCE_STALE"),
        ("future", "EVIDENCE_FROM_FUTURE"),
    ],
)
def test_one_millisecond_invalid_evidence_refuses(kind, reason):
    module = _module()
    payload = _payload(module)
    candidate = _candidate("bad")
    if kind == "deadline":
        candidate["deadline"] = "2026-08-26T11:59:59.999Z"
    elif kind == "stale":
        candidate["evidence_observed_at"] = "2026-08-26T11:59:58.999Z"
    else:
        candidate["evidence_observed_at"] = "2026-08-26T12:00:00.001Z"
    payload["candidates"] = [candidate]
    _rehash(module, payload)
    row = module.build_report(payload)["refused"][0]
    assert reason in row["reasons"]
    assert row["status"] == "REFUSE"


@pytest.mark.parametrize(
    "kind", ["empty", "duplicate", "timestamp", "offset", "hash", "freshness", "work", "fields"]
)
def test_malformed_schedule_evidence_fails_closed(kind):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["candidates"] = []
    elif kind == "duplicate":
        payload["candidates"].append(copy.deepcopy(payload["candidates"][0]))
    elif kind == "timestamp":
        payload["candidates"][0]["deadline"] = "bad"
    elif kind == "offset":
        payload["as_of"] = "2026-08-26T06:00:00-06:00"
    elif kind == "hash":
        payload["candidates"][0]["evidence_hash"] = "bad"
    elif kind == "freshness":
        payload["candidates"][0]["freshness_limit_ms"] = True
    elif kind == "work":
        payload["candidates"][0]["work_units"] = 0
    else:
        payload["candidates"][0]["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_input_order_is_irrelevant_and_not_mutated():
    module = _module()
    first = module.build_report(_payload(module))
    payload = _payload(module)
    payload["candidates"].reverse()
    _rehash(module, payload)
    original = copy.deepcopy(payload)
    second = module.build_report(payload)
    assert first["schedule"] == second["schedule"]
    assert payload == original


def test_tampering_determinism_and_atomic_publication(tmp_path: Path):
    module = _module()
    payload = _payload(module)
    payload["as_of"] = "2026-08-26T12:00:00.001Z"
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "schedule.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_trading_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ei_deterministic_candidate_scheduling.py"
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
