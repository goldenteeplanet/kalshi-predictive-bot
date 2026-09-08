from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cj_stale_snapshot_refusal.py"
    spec = importlib.util.spec_from_file_location("phase4cj_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "evaluated_at": "2026-08-26T00:00:01.001000Z",
        "max_age_ms": 1000,
        "snapshots": [
            {
                "snapshot_id": "boundary",
                "captured_at": "2026-08-26T00:00:00.001000Z",
                "content_hash": "a" * 64,
            },
            {
                "snapshot_id": "stale",
                "captured_at": "2026-08-26T00:00:00Z",
                "content_hash": "b" * 64,
            },
            {
                "snapshot_id": "future",
                "captured_at": "2026-08-26T00:00:02Z",
                "content_hash": "c" * 64,
            },
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_exact_boundary_is_accepted_and_one_ms_over_is_refused():
    module = _module()
    report = module.build_report(_payload(module))
    rows = {row["snapshot_id"]: row for row in report["decisions"]}
    assert rows["boundary"]["age_ms"] == 1000
    assert rows["boundary"]["decision"] == "ACCEPT"
    assert rows["stale"]["age_ms"] == 1001
    assert rows["stale"]["reason"] == "STALE_SNAPSHOT"
    assert rows["future"]["reason"] == "FUTURE_SNAPSHOT"
    assert report["early_refusal_count"] == 2
    assert report["accepted_count"] == 1


def test_results_are_deterministic_and_preserve_input_order():
    module = _module()
    first = module.build_report(_payload(module))
    assert first == module.build_report(_payload(module))
    assert [row["snapshot_id"] for row in first["decisions"]] == [
        "boundary",
        "stale",
        "future",
    ]


@pytest.mark.parametrize("value", [-1, 86_400_001, True])
def test_invalid_max_age_fails_closed(value):
    module = _module()
    payload = _payload(module)
    payload["max_age_ms"] = value
    _rehash(module, payload)
    with pytest.raises(ValueError, match="MAX_AGE"):
        module.build_report(payload)


@pytest.mark.parametrize("kind", ["empty", "fields", "duplicate", "hash", "timestamp"])
def test_invalid_snapshot_evidence_fails_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["snapshots"] = []
    elif kind == "fields":
        payload["snapshots"][0]["extra"] = True
    elif kind == "duplicate":
        payload["snapshots"][1]["snapshot_id"] = "boundary"
    elif kind == "hash":
        payload["snapshots"][0]["content_hash"] = "not-a-hash"
    else:
        payload["snapshots"][0]["captured_at"] = "yesterday"
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_tampering_and_unknown_fields_fail_closed():
    module = _module()
    payload = _payload(module)
    payload["max_age_ms"] = 999
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)
    payload = _payload(module)
    payload["extra"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError, match="FIELDS"):
        module.build_report(payload)


def test_atomic_publication(tmp_path: Path):
    module = _module()
    report = module.build_report(_payload(module))
    output = tmp_path / "refusal.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_mutation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cj_stale_snapshot_refusal.py"
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
