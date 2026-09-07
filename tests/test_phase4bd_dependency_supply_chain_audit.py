from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bd_dependency_supply_chain_audit.py"
    spec = importlib.util.spec_from_file_location("phase4bd_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "locks").mkdir()
    (root / "provenance").mkdir()
    (root / "src" / "guarded.py").write_text("import json\nimport fixturedep\n")
    lock = root / "locks" / "requirements.lock"
    lock.write_text("fixturedep==1.2.3 --hash=sha256:local\n")
    provenance_payload = {"name": "fixturedep", "version": "1.2.3", "source": "local-wheel-cache"}
    provenance = root / "provenance" / "fixturedep.json"
    provenance.write_text(json.dumps(provenance_payload))
    audit = {
        "schema": module.INPUT_SCHEMA,
        "source_paths": ["src/guarded.py"],
        "stdlib_imports": ["json"],
        "dependencies": [
            {
                "name": "fixturedep",
                "import_name": "fixturedep",
                "locked_version": "1.2.3",
                "lock_source": "locks/requirements.lock",
                "expected_lock_hash": module.canonical_hash(lock.read_bytes().hex()),
                "local_provenance": "provenance/fixturedep.json",
                "expected_provenance_hash": module.canonical_hash(provenance_payload),
                "executable_hooks": [],
                "generated_files": [],
                "capabilities": ["READ_ONLY"],
            }
        ],
    }
    audit["artifact_hash"] = module._hash(audit)
    audit_path = root / "audit.json"
    audit_path.write_text(json.dumps(audit))
    return module, root, audit_path, lock, provenance


def _mutate(module, path: Path, mutate, *, rehash=True):
    payload = json.loads(path.read_text())
    mutate(payload)
    if rehash:
        payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload))


def test_local_locked_dependency_provenance_and_usage_pass(tmp_path: Path):
    module, root, audit, _, _ = _fixture(tmp_path)
    sbom, risk = module.build(root, audit, now=NOW)
    assert sbom["component_count"] == 1
    assert sbom["components"][0]["integrity_verified"] is True
    assert risk["dependency_integrity_verified"] is True
    assert sbom["network_access_performed"] is False
    assert sbom["installed_or_upgraded_dependencies"] is False
    assert risk["artifact_hash"] == module._hash(risk)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda p: p["dependencies"][0].update(executable_hooks=["setup.py"]),
            "UNEXPECTED_EXECUTABLE_HOOK",
        ),
        (
            lambda p: p["dependencies"][0].update(capabilities=["NETWORK"]),
            "MUTATION_OR_NETWORK_CAPABLE_DEPENDENCY",
        ),
        (
            lambda p: p["dependencies"][0].update(capabilities=["DATABASE_MUTATION"]),
            "MUTATION_OR_NETWORK_CAPABLE_DEPENDENCY",
        ),
    ],
)
def test_hooks_mutation_network_and_unused_dependencies_are_risks(
    tmp_path: Path, mutation, reason: str
):
    module, root, audit, _, _ = _fixture(tmp_path)
    _mutate(module, audit, mutation)
    _, risk = module.build(root, audit, now=NOW)
    assert reason in {finding["reason"] for finding in risk["findings"]}
    assert risk["dependency_integrity_verified"] is False


def test_declared_but_unused_dependency_is_a_risk(tmp_path: Path):
    module, root, audit, _, _ = _fixture(tmp_path)
    (root / "src" / "guarded.py").write_text("import json\n")
    _, risk = module.build(root, audit, now=NOW)
    assert "DEPENDENCY_NOT_USED_BY_GUARDED_SOURCES" in {
        finding["reason"] for finding in risk["findings"]
    }


def test_lock_provenance_and_version_drift_are_detected(tmp_path: Path):
    module, root, audit, lock, provenance = _fixture(tmp_path / "lock")
    lock.write_text("fixturedep==9.9.9\n")
    _, risk = module.build(root, audit, now=NOW)
    assert "DEPENDENCY_LOCK_DRIFT" in {finding["reason"] for finding in risk["findings"]}
    module, root, audit, _, provenance = _fixture(tmp_path / "provenance")
    provenance.write_text(json.dumps({"name": "fixturedep", "version": "9.9.9"}))
    _, risk = module.build(root, audit, now=NOW)
    reasons = {finding["reason"] for finding in risk["findings"]}
    assert {"DEPENDENCY_PROVENANCE_DRIFT", "DEPENDENCY_VERSION_DRIFT"} <= reasons


def test_unexplained_import_duplicate_dependency_and_tampering_fail_closed(tmp_path: Path):
    module, root, audit, _, _ = _fixture(tmp_path / "import")
    (root / "src" / "guarded.py").write_text("import unexpected\nimport fixturedep\n")
    with pytest.raises(ValueError, match="UNEXPLAINED_IMPORTS"):
        module.build(root, audit, now=NOW)
    module, root, audit, _, _ = _fixture(tmp_path / "duplicate")
    _mutate(module, audit, lambda p: p["dependencies"].append(dict(p["dependencies"][0])))
    with pytest.raises(ValueError, match="DUPLICATE_DEPENDENCY"):
        module.build(root, audit, now=NOW)
    module, root, audit, _, _ = _fixture(tmp_path / "tamper")
    _mutate(module, audit, lambda p: p.update(extra=True), rehash=False)
    with pytest.raises(ValueError, match="INPUT_SCHEMA_OR_HASH_INVALID"):
        module.build(root, audit, now=NOW)


def test_path_symlink_and_time_fail_closed(tmp_path: Path):
    module, root, audit, _, provenance = _fixture(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(provenance.read_text())
    _mutate(
        module,
        audit,
        lambda p: p["dependencies"][0].update(local_provenance=str(outside)),
    )
    with pytest.raises(ValueError, match="PROVENANCE_PATH_INVALID"):
        module.build(root, audit, now=NOW)
    module, root, audit, _, _ = _fixture(tmp_path / "time")
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(root, audit, now=datetime(2026, 8, 25, 12, 0))


def test_static_audit_has_no_install_upgrade_network_or_subprocess_path():
    source = (
        Path(__file__).parents[1] / "scripts/local/phase4bd_dependency_supply_chain_audit.py"
    ).read_text()
    assert "subprocess" not in source
    assert "pip install" not in source
    assert "urllib" not in source and "requests" not in source
    assert "sqlite3" not in source
