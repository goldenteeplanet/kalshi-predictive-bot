from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCRIPT = Path(__file__).parents[1] / "scripts/local/phase4ev_safety_critical_mutation_scanner.py"
SPEC = importlib.util.spec_from_file_location("phase4ev", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _seal(value):
    value.pop("artifact_hash", None)
    value["artifact_hash"] = canonical_hash(value)
    return value


def _repo(tmp_path, sources):
    root = tmp_path / "repo"
    local = root / "scripts/local"
    local.mkdir(parents=True)
    for name, source in sources.items():
        (local / name).write_text(source)
    return root


def _manifest(root):
    files = []
    for path in sorted((root / "scripts/local").glob(MODULE.DISCOVERY_GLOB)):
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return _seal({"schema": MODULE.MANIFEST_SCHEMA, "files": files})


def _scan(tmp_path, source="value = 1\n"):
    root = _repo(tmp_path, {"phase4ea_safe.py": source})
    return MODULE.build_report(root, _manifest(root))


def test_clean_complete_manifest_passes(tmp_path):
    report = _scan(tmp_path)
    assert report["scanned_file_count"] == report["expected_file_count"] == 1
    assert report["findings"] == []
    assert report["dynamic_dispatch_checked"] is True
    assert report["indirect_adapters_checked"] is True
    assert report["advancement_allowed"] is True


@pytest.mark.parametrize(
    ("source", "capability"),
    [
        ("session.commit()\n", "DIRECT_MUTATION_CALL"),
        ("connection.execute(sql)\n", "DIRECT_MUTATION_CALL"),
        ("client.create_order(order)\n", "DIRECT_MUTATION_CALL"),
        ("service.restart()\n", "DIRECT_MUTATION_CALL"),
        ("import sqlite3\nsqlite3.connect('x.db')\n", "WRITABLE_DATABASE_CONNECTION"),
        ("import subprocess\nsubprocess.run(['x'])\n", "SUBPROCESS_CONTROL_SURFACE"),
        ("eval(source)\n", "DYNAMIC_CODE_EXECUTION"),
        ("exec(source)\n", "DYNAMIC_CODE_EXECUTION"),
        ("__import__(name)\n", "DYNAMIC_IMPORT"),
        ("import importlib\nimportlib.import_module(name)\n", "DYNAMIC_IMPORT"),
        ("getattr(client, method)(payload)\n", "DYNAMIC_GETATTR_DISPATCH"),
        ("registry[key](payload)\n", "REGISTRY_OR_MAPPING_DISPATCH"),
        ("from pkg.exchange_adapter import Client\n", "INDIRECT_MUTATION_ADAPTER_IMPORT"),
        ("from pkg import order_writer\n", "INDIRECT_MUTATION_ADAPTER_IMPORT"),
        ("def route_order(value):\n    return value\n", "MUTATION_ENTRYPOINT_DEFINITION"),
        ("runner = client.place_order\nrunner(payload)\n", "MUTATION_CALLABLE_ALIAS"),
    ],
)
def test_each_direct_dynamic_and_indirect_surface_closes_gate(tmp_path, source, capability):
    report = _scan(tmp_path, source)
    assert capability in {item["capability"] for item in report["findings"]}
    assert report["all_mutation_surfaces_absent"] is False
    assert report["advancement_allowed"] is False


def test_read_only_sqlite_connection_is_allowed(tmp_path):
    report = _scan(tmp_path, "import sqlite3\nsqlite3.connect('file:x?mode=ro', uri=True)\n")
    assert report["findings"] == []


def test_missing_discovered_file_in_manifest_fails_closed(tmp_path):
    root = _repo(tmp_path, {"phase4ea_a.py": "x=1\n", "phase4eb_b.py": "x=2\n"})
    manifest = _manifest(root)
    manifest["files"].pop()
    _seal(manifest)
    with pytest.raises(ValueError, match="COVERAGE_MISMATCH"):
        MODULE.build_report(root, manifest)


def test_extra_manifest_file_fails_closed(tmp_path):
    root = _repo(tmp_path, {"phase4ea_a.py": "x=1\n"})
    manifest = _manifest(root)
    manifest["files"].append({"path": "scripts/local/phase4eb_missing.py", "sha256": "a" * 64})
    _seal(manifest)
    with pytest.raises(ValueError, match="COVERAGE_MISMATCH"):
        MODULE.build_report(root, manifest)


def test_duplicate_manifest_path_fails_closed(tmp_path):
    root = _repo(tmp_path, {"phase4ea_a.py": "x=1\n"})
    manifest = _manifest(root)
    manifest["files"].append(dict(manifest["files"][0]))
    _seal(manifest)
    with pytest.raises(ValueError, match="MANIFEST_PATH"):
        MODULE.build_report(root, manifest)


def test_file_tampering_after_manifest_fails_closed(tmp_path):
    root = _repo(tmp_path, {"phase4ea_a.py": "x=1\n"})
    manifest = _manifest(root)
    (root / "scripts/local/phase4ea_a.py").write_text("x=2\n")
    with pytest.raises(ValueError, match="FILE_HASH_MISMATCH"):
        MODULE.build_report(root, manifest)


def test_manifest_tampering_fails_closed(tmp_path):
    root = _repo(tmp_path, {"phase4ea_a.py": "x=1\n"})
    manifest = _manifest(root)
    manifest["files"][0]["sha256"] = "a" * 64
    with pytest.raises(ValueError, match="SCHEMA_OR_HASH"):
        MODULE.build_report(root, manifest)


def test_syntax_error_fails_closed(tmp_path):
    root = _repo(tmp_path, {"phase4ea_a.py": "def broken(\n"})
    with pytest.raises(ValueError, match="SOURCE_INVALID"):
        MODULE.build_report(root, _manifest(root))


def test_actual_workstream_iv_files_are_complete_and_clean():
    root = Path(__file__).parents[1]
    manifest = _manifest(root)
    report = MODULE.build_report(root, manifest)
    assert report["scanned_file_count"] >= 21
    assert report["scanned_file_count"] == report["expected_file_count"]
    assert report["findings"] == []
    assert report["advancement_allowed"] is True


def test_atomic_publication(tmp_path):
    report = _scan(tmp_path)
    output = tmp_path / "scan.json"
    MODULE.publish(output, report)
    assert json.loads(output.read_text()) == report
    assert not list(tmp_path.glob(".*"))


def test_report_never_mutates_or_authorizes(tmp_path):
    report = _scan(tmp_path)
    assert report["production_database_mutated"] is False
    assert report["services_controlled"] is False
    assert report["exchange_requests_made"] is False
    assert report["paper_orders_created"] == 0
    assert report["execution_authorized"] is False
