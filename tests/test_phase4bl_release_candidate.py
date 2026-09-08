from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bl_release_candidate.py"
    spec = importlib.util.spec_from_file_location("phase4bl_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    inventory = {}
    for category in module.REQUIRED:
        path = tmp_path / f"{category}.txt"
        path.write_text(f"offline {category} evidence", encoding="utf-8")
        inventory[category] = [path.name]
    return module, inventory


def test_release_candidate_is_complete_reproducible_and_non_authorizing(tmp_path: Path):
    module, inventory = _fixture(tmp_path)
    first = module.build(tmp_path, inventory)
    second = module.build(tmp_path, inventory)
    assert first == second
    manifest, safety = first
    assert manifest["file_count"] == len(module.REQUIRED)
    assert {row["category"] for row in manifest["files"]} == set(module.REQUIRED)
    assert safety["local_undeployed"] is True
    assert safety["execution_authorized"] is False


@pytest.mark.parametrize("missing", _module().REQUIRED)
def test_each_required_category_is_fail_closed(tmp_path: Path, missing: str):
    module, inventory = _fixture(tmp_path)
    inventory.pop(missing)
    with pytest.raises(ValueError, match="COVERAGE"):
        module.build(tmp_path, inventory)


@pytest.mark.parametrize(
    ("name", "content", "reason"),
    (("service-unit.txt", "offline", "PATH"), ("safe.txt", "systemctl start x", "CAPABILITY")),
)
def test_prohibited_paths_and_content_fail_closed(
    tmp_path: Path, name: str, content: str, reason: str
):
    module, inventory = _fixture(tmp_path)
    (tmp_path / name).write_text(content, encoding="utf-8")
    inventory["verifier"] = [name]
    with pytest.raises(ValueError, match=reason):
        module.build(tmp_path, inventory)


def test_duplicates_traversal_missing_symlink_and_drift(tmp_path: Path):
    module, inventory = _fixture(tmp_path)
    inventory["simulator"] = inventory["verifier"]
    with pytest.raises(ValueError, match="DUPLICATE"):
        module.build(tmp_path, inventory)
    module, inventory = _fixture(tmp_path / "traversal")
    inventory["verifier"] = ["../outside.txt"]
    with pytest.raises(ValueError, match="PATH_INVALID"):
        module.build(tmp_path / "traversal", inventory)
    module, inventory = _fixture(tmp_path / "missing")
    inventory["verifier"] = ["absent.txt"]
    with pytest.raises(ValueError, match="FILE_INVALID"):
        module.build(tmp_path / "missing", inventory)
    module, inventory = _fixture(tmp_path / "drift")
    first, _ = module.build(tmp_path / "drift", inventory)
    (tmp_path / "drift" / inventory["tests"][0]).write_text("changed")
    second, _ = module.build(tmp_path / "drift", inventory)
    assert first["artifact_hash"] != second["artifact_hash"]
