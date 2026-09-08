from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4ck_snapshot_serialization.py"
    spec = importlib.util.spec_from_file_location("phase4ck_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(module):
    payload = {
        "schema": module.INPUT_SCHEMA,
        "snapshots": [
            {
                "snapshot_id": "s-1",
                "market_ticker": "KXTEST",
                "sequence": 7,
                "yes_bids": [[55, 10], [54, 20]],
                "no_bids": [[44, 12]],
            }
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_compact_encoding_preserves_semantics_and_saves_bytes():
    module = _module()
    report = module.build_report(_payload(module))
    assert report["round_trip_equal"] is True
    assert report["compact_bytes"] < report["baseline_bytes"]
    assert report["bytes_saved"] == report["baseline_bytes"] - report["compact_bytes"]
    assert report["optimization_applied_to_runtime"] is False


def test_key_order_does_not_change_semantic_hash():
    module = _module()
    first = _payload(module)
    snapshot = first["snapshots"][0]
    first_report = module.build_report(first)
    reordered = {
        "schema": module.INPUT_SCHEMA,
        "snapshots": [{key: snapshot[key] for key in reversed(tuple(snapshot))}],
    }
    _rehash(module, reordered)
    assert module.build_report(reordered)["semantic_hash"] == first_report["semantic_hash"]


@pytest.mark.parametrize("kind", ["empty", "too_many", "fields", "duplicate", "ticker", "sequence"])
def test_invalid_snapshot_shapes_fail_closed(kind: str):
    module = _module()
    payload = _payload(module)
    if kind == "empty":
        payload["snapshots"] = []
    elif kind == "too_many":
        payload["snapshots"] = [payload["snapshots"][0]] * (module.MAX_SNAPSHOTS + 1)
    elif kind == "fields":
        payload["snapshots"][0]["extra"] = True
    elif kind == "duplicate":
        payload["snapshots"].append(dict(payload["snapshots"][0]))
    elif kind == "ticker":
        payload["snapshots"][0]["market_ticker"] = ""
    else:
        payload["snapshots"][0]["sequence"] = True
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


@pytest.mark.parametrize("levels", ["bad", [[101, 1]], [[50, 0]], [[50]], [[50, True]]])
def test_invalid_levels_fail_closed(levels):
    module = _module()
    payload = _payload(module)
    payload["snapshots"][0]["yes_bids"] = levels
    _rehash(module, payload)
    with pytest.raises(ValueError, match="LEVEL"):
        module.build_report(payload)


def test_tampering_fails_closed():
    module = _module()
    payload = _payload(module)
    payload["snapshots"][0]["sequence"] = 8
    with pytest.raises(ValueError, match="HASH"):
        module.build_report(payload)


def test_deterministic_atomic_publication(tmp_path: Path):
    module = _module()
    report = module.build_report(_payload(module))
    assert report == module.build_report(_payload(module))
    output = tmp_path / "serialization.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_connected_or_mutation_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4ck_snapshot_serialization.py"
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
