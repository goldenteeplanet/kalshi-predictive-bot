from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bx_algorithmic_hotspot_audit.py"
    spec = importlib.util.spec_from_file_location("phase4bx_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOURCE = """\
import json
from datetime import datetime
def work(rows):
    parsed = [json.loads(row) for row in rows]
    for row in parsed:
        for value in row:
            canonical_hash(value)
    return json.dumps(sorted(parsed)), datetime.fromisoformat('2026-01-01')
"""


def _inventory(module, sources=None):
    sources = sources or [("a.py", SOURCE)]
    rows = [
        {"name": name, "source": source, "source_hash": module.canonical_hash(source)}
        for name, source in sources
    ]
    payload = {"schema": module.INPUT_SCHEMA, "sources": rows}
    payload["artifact_hash"] = module._hash(payload)
    return payload


def test_audit_is_deterministic_complete_and_non_authorizing():
    module = _module()
    report = module.build_report(_inventory(module))
    assert report == module.build_report(_inventory(module))
    assert report["category_totals"] == {
        "NESTED_ITERATION": 1,
        "REPEATED_PARSING": 2,
        "REPEATED_HASHING": 1,
        "REDUNDANT_SORTING": 1,
        "EXCESSIVE_SERIALIZATION": 1,
    }
    assert report["source_mutations_applied"] == 0
    assert report["execution_authorized"] is False


def test_clean_source_has_zero_findings():
    module = _module()
    report = module.build_report(_inventory(module, [("clean.py", "answer = 42\n")]))
    assert report["finding_count"] == 0


def test_inventory_from_paths_is_sorted_and_hash_protected(tmp_path: Path):
    module = _module()
    second, first = tmp_path / "b.py", tmp_path / "a.py"
    second.write_text("b = 2\n")
    first.write_text("a = 1\n")
    inventory = module.inventory_from_paths([second, first])
    assert [row["name"] for row in inventory["sources"]] == sorted(
        [first.as_posix(), second.as_posix()]
    )
    module.build_report(inventory)


@pytest.mark.parametrize("kind", ("outer", "source", "fields", "duplicate", "order", "syntax"))
def test_invalid_inventory_fails_closed(kind: str):
    module = _module()
    payload = _inventory(module, [("a.py", "x = 1\n"), ("b.py", "y = 2\n")])
    if kind == "outer":
        payload["extra"] = True
    elif kind == "source":
        payload["sources"][0]["source"] = "x = 2\n"
        payload["artifact_hash"] = module._hash(payload)
    elif kind == "fields":
        payload["sources"][0]["extra"] = True
        payload["artifact_hash"] = module._hash(payload)
    elif kind == "duplicate":
        payload["sources"][1]["name"] = "a.py"
        payload["artifact_hash"] = module._hash(payload)
    elif kind == "order":
        payload["sources"].reverse()
        payload["artifact_hash"] = module._hash(payload)
    else:
        payload["sources"][0]["source"] = "def bad("
        payload["sources"][0]["source_hash"] = module.canonical_hash("def bad(")
        payload["artifact_hash"] = module._hash(payload)
    with pytest.raises(ValueError):
        module.build_report(payload)


def test_duplicate_path_fails_closed(tmp_path: Path):
    module = _module()
    path = tmp_path / "a.py"
    path.write_text("x = 1\n")
    with pytest.raises(ValueError):
        module.inventory_from_paths([path, path])


def test_atomic_publication_round_trip(tmp_path: Path):
    module = _module()
    report = module.build_report(_inventory(module))
    output = tmp_path / "audit.json"
    module.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_source_has_no_database_service_network_or_exchange_surface():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bx_algorithmic_hotspot_audit.py"
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
