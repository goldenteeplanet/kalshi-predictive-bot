from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4as_mutation_surface_scanner.py"
    spec = importlib.util.spec_from_file_location("phase4as_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _allowlist(module, path: Path, entries: list[dict] | None = None):
    payload = {"schema": module.ALLOWLIST_SCHEMA, "entries": entries or []}
    payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload))
    return path


def _scan(tmp_path: Path, sources: dict[str, str], entries=None):
    module = _module()
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    for name, source in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    allowlist = _allowlist(module, tmp_path / "allowlist.json", entries)
    return module, module.build(root, allowlist, now=NOW)


def test_clean_read_only_repository_passes(tmp_path: Path):
    module, (inventory, verdict) = _scan(
        tmp_path,
        {"audit.py": "import sqlite3\nc=sqlite3.connect(f'file:{p}?mode=ro',uri=True)\n"},
    )
    assert inventory["findings"] == []
    assert verdict["verdict"] == "MUTATION_SURFACES_EXPLAINED"
    assert verdict["advancement_allowed"] is True
    assert inventory["artifact_hash"] == module._hash(inventory)


@pytest.mark.parametrize(
    ("source", "capability"),
    [
        ("import sqlite3\nc=sqlite3.connect('x.db')\n", "WRITABLE_SQLITE_CONNECTION"),
        ("SQL='UPDATE settlements SET settled_at=1'\n", "SETTLEMENT_UPDATE_SQL"),
        ("SQL='INSERT INTO settlements VALUES (1)'\n", "SETTLEMENT_INSERT_SQL"),
        ("SQL='DELETE FROM settlements'\n", "SETTLEMENT_DELETE_SQL"),
        ("# settlement\nsession.add(value)\n", "ORM_OR_DYNAMIC_MUTATION"),
        ("# settlement\nconnection.execute(sql)\n", "ORM_OR_DYNAMIC_MUTATION"),
        (
            "# settlement\nimport subprocess\nsubprocess.run(['mutate'])\n",
            "SUBPROCESS_MUTATION_COMMAND",
        ),
        ("# settlement\nimport fcntl\nfcntl.flock(1,2)\n", "WRITER_LOCK_ACQUISITION"),
        ("# settlement\nfrom alembic import op\nop.execute('x')\n", "MIGRATION_PATH"),
    ],
)
def test_each_python_mutation_surface_fails_closed(tmp_path: Path, source: str, capability: str):
    _, (inventory, verdict) = _scan(tmp_path, {"surface.py": source})
    assert capability in {row["capability"] for row in inventory["findings"]}
    assert verdict["verdict"] == "UNEXPLAINED_MUTATION_SURFACE"
    assert verdict["advancement_allowed"] is False


def test_mutating_main_guard_is_hidden_entrypoint(tmp_path: Path):
    source = "SQL='UPDATE settlements SET settled_at=1'\nif __name__ == '__main__':\n print(SQL)\n"
    _, (inventory, _) = _scan(tmp_path, {"executor.py": source})
    assert "MUTATION_CAPABLE_ENTRYPOINT" in {row["capability"] for row in inventory["findings"]}


def test_shell_service_lock_and_sqlite_mutation_detected(tmp_path: Path):
    source = (
        "systemctl restart settlement-service\n"
        "flock /tmp/writer.lock sqlite3 x.db 'update x set y=1'\n"
    )
    _, (inventory, verdict) = _scan(tmp_path, {"run.sh": source})
    capabilities = {row["capability"] for row in inventory["findings"]}
    assert {
        "SERVICE_SCRIPT_CONTROL",
        "WRITER_LOCK_ACQUISITION",
        "SUBPROCESS_MUTATION_COMMAND",
    } <= capabilities
    assert verdict["advancement_allowed"] is False


def test_exact_safe_scope_allowlist_explains_finding(tmp_path: Path):
    entries = [
        {
            "path": "simulation.py",
            "capability": "SETTLEMENT_UPDATE_SQL",
            "scope": "DISPOSABLE_SIMULATION",
            "justification": "Marker-gated disposable fixture only.",
        }
    ]
    _, (inventory, verdict) = _scan(
        tmp_path, {"simulation.py": "SQL='UPDATE settlements SET settled_at=1'\n"}, entries
    )
    assert inventory["findings"][0]["classification"] == "ALLOWLISTED_SAFE_SCOPE"
    assert verdict["advancement_allowed"] is True


def test_allowlist_rejects_production_scope_duplicate_and_empty_justification(tmp_path: Path):
    module = _module()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x.py").write_text("x=1\n")
    base = {"path": "x.py", "capability": "X", "scope": "PRODUCTION", "justification": "x"}
    path = _allowlist(module, tmp_path / "allowlist.json", [base])
    with pytest.raises(ValueError, match="ENTRY_INVALID"):
        module.build(root, path, now=NOW)
    base["scope"] = "TEST"
    base["justification"] = ""
    path = _allowlist(module, path, [base])
    with pytest.raises(ValueError, match="ENTRY_INVALID"):
        module.build(root, path, now=NOW)
    base["justification"] = "fixture"
    path = _allowlist(module, path, [base, dict(base)])
    with pytest.raises(ValueError, match="DUPLICATED"):
        module.build(root, path, now=NOW)


def test_stale_allowlist_entry_fails_closed(tmp_path: Path):
    entries = [
        {
            "path": "missing.py",
            "capability": "SETTLEMENT_UPDATE_SQL",
            "scope": "TEST",
            "justification": "No longer present.",
        }
    ]
    _, (_, verdict) = _scan(tmp_path, {"safe.py": "x=1\n"}, entries)
    assert verdict["stale_allowlist_entries"]
    assert verdict["advancement_allowed"] is False


def test_tampered_allowlist_and_invalid_python_fail_closed(tmp_path: Path):
    module = _module()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "safe.py").write_text("x=1\n")
    allowlist = _allowlist(module, tmp_path / "allowlist.json")
    payload = json.loads(allowlist.read_text())
    payload["tampered"] = True
    allowlist.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="HASH_INVALID"):
        module.build(root, allowlist, now=NOW)
    _allowlist(module, allowlist)
    (root / "broken.py").write_text("def broken(:\n")
    with pytest.raises(ValueError, match="SOURCE_INVALID"):
        module.build(root, allowlist, now=NOW)


def test_deterministic_hash_valid_non_authorizing_outputs(tmp_path: Path):
    module, first = _scan(tmp_path, {"safe.py": "x=1\n"})
    _, second = _scan(tmp_path / "again", {"safe.py": "x=1\n"})
    # Root identity intentionally differs; stable content-derived fields remain equal.
    assert first[0]["scanned_files_hash"] == second[0]["scanned_files_hash"]
    assert first[0]["findings"] == second[0]["findings"]
    inventory, verdict = first
    assert inventory["artifact_hash"] == module._hash(inventory)
    assert verdict["manifest_hash"] == module._hash(verdict, "manifest_hash")
    rendered = json.dumps(first, sort_keys=True).lower()
    assert 'execution_authorized": true' not in rendered
    assert 'production_database_mutated": true' not in rendered
