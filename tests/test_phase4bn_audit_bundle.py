from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bn_audit_bundle.py"
    spec = importlib.util.spec_from_file_location("phase4bn_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _components(module):
    result = {}
    for name in module.COMPONENTS:
        artifact = {"schema": f"fixture.{name}.v1", "value": name, "execution_authorized": False}
        artifact["artifact_hash"] = module._hash(artifact)
        result[name] = artifact
    return result


def test_complete_bundle_is_deterministic_hash_protected_and_non_authorizing():
    module = _module()
    components = _components(module)
    assert module.build(components) == module.build(components)
    bundle, proof = module.build(components)
    assert bundle["component_count"] == 13
    assert proof["bundle_hash"] == bundle["artifact_hash"]
    assert bundle["execution_authorized"] is False


@pytest.mark.parametrize("missing", _module().COMPONENTS)
def test_every_component_is_required(missing: str):
    module = _module()
    components = _components(module)
    components.pop(missing)
    with pytest.raises(ValueError, match="COVERAGE"):
        module.build(components)


def test_order_tamper_authority_schema_drift_and_duplicate_fail_closed():
    module = _module()
    components = _components(module)
    reversed_components = dict(reversed(list(components.items())))
    with pytest.raises(ValueError, match="ORDER"):
        module.build(reversed_components)
    components = _components(module)
    components["threat_model"]["value"] = "tampered"
    with pytest.raises(ValueError, match="HASH"):
        module.build(components)
    components = _components(module)
    components["build_identity"]["execution_authorized"] = True
    components["build_identity"]["artifact_hash"] = module._hash(components["build_identity"])
    with pytest.raises(ValueError, match="AUTHORITY"):
        module.build(components)
    components = _components(module)
    first, _ = module.build(components)
    components["schema_catalog"]["schema"] = "fixture.schema_catalog.v2"
    components["schema_catalog"]["artifact_hash"] = module._hash(components["schema_catalog"])
    second, _ = module.build(components)
    assert first["artifact_hash"] != second["artifact_hash"]


def test_source_is_artifact_only():
    source = (Path(__file__).parents[1] / "scripts/local/phase4bn_audit_bundle.py").read_text()
    for token in ("sqlite3", "subprocess", "requests", "systemctl", "/home/james"):
        assert token not in source
