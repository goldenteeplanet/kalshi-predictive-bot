from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4cd_snapshot_deduplication_index.py"
    spec = importlib.util.spec_from_file_location("phase4cd_index_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _book(offset=0):
    return {"yes": [[40 + offset, 3], [30 + offset, 2]], "no": [[60 - offset, 4]]}


def _payload(module):
    first_hash = module.canonical_hash(module._book(_book()))
    payload = {
        "schema": module.INPUT_SCHEMA,
        "prior_index": [{"market_id": "A", "sequence": 4, "content_hash": first_hash}],
        "snapshots": [
            {"market_id": "A", "sequence": 5, "order_book": _book()},
            {"market_id": "A", "sequence": 6, "order_book": _book(1)},
            {"market_id": "B", "sequence": 1, "order_book": _book()},
        ],
    }
    payload["artifact_hash"] = module._hash(payload)
    return payload


def _rehash(module, payload):
    payload["artifact_hash"] = module._hash(payload)


def test_index_is_deterministic_and_skips_only_unchanged_books():
    module = _module()
    result = module.build_index(_payload(module))
    assert result == module.build_index(_payload(module))
    assert [row["decision"] for row in result["decisions"]] == [
        "SKIP_UNCHANGED",
        "RECOMPUTE",
        "RECOMPUTE",
    ]
    assert result["skipped_unchanged_count"] == 1
    assert result["recompute_count"] == 2
    assert [row["market_id"] for row in result["resulting_index"]] == ["A", "B"]
    assert result["execution_authorized"] is False


def test_level_order_does_not_change_content_identity():
    module = _module()
    first = module._book(_book())
    reversed_book = {"yes": list(reversed(_book()["yes"])), "no": _book()["no"]}
    assert first == module._book(reversed_book)


@pytest.mark.parametrize("kind", ("fields", "side", "level", "value", "price", "quantity"))
def test_malformed_order_book_fails_closed(kind: str):
    module = _module()
    payload = _payload(module)
    book = payload["snapshots"][0]["order_book"]
    if kind == "fields":
        payload["snapshots"][0]["extra"] = True
    elif kind == "side":
        book["other"] = []
    elif kind == "level":
        book["yes"][0] = [40]
    elif kind == "value":
        book["yes"][0][0] = True
    elif kind == "price":
        book["yes"][0][0] = 101
    else:
        book["yes"][0][1] = 0
    _rehash(module, payload)
    with pytest.raises(ValueError):
        module.build_index(payload)


def test_sequence_regression_and_duplicate_fail_closed():
    module = _module()
    for sequence in (4, 3):
        payload = _payload(module)
        payload["snapshots"][0]["sequence"] = sequence
        _rehash(module, payload)
        with pytest.raises(ValueError, match="SEQUENCE"):
            module.build_index(payload)


def test_collision_fails_closed(monkeypatch):
    module = _module()
    payload = _payload(module)
    payload["prior_index"] = []
    monkeypatch.setattr(module, "canonical_hash", lambda value: "0" * 64)
    payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError, match="COLLISION"):
        module.build_index(payload)


def test_outer_tampering_fails_closed():
    module = _module()
    payload = _payload(module)
    payload["extra"] = True
    with pytest.raises(ValueError, match="HASH"):
        module.build_index(payload)


def test_atomic_publication_round_trip(tmp_path: Path):
    module = _module()
    result = module.build_index(_payload(module))
    output = tmp_path / "result.json"
    module.publish(output, result)
    assert json.loads(output.read_text()) == result
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4cd_snapshot_deduplication_index.py"
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
