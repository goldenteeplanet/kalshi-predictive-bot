import importlib.util
import json
from pathlib import Path

import pytest

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

S = Path(__file__).parents[1] / "scripts/local/phase4ff_changed_file_selector.py"
X = importlib.util.spec_from_file_location("m", S)
assert X and X.loader
M = importlib.util.module_from_spec(X)
X.loader.exec_module(M)


def seal(v):
    v.pop("artifact_hash", None)
    v["artifact_hash"] = canonical_hash(v)
    return v


def payload():
    return seal(
        {
            "schema": M.SCHEMA,
            "cache_hash": "a" * 64,
            "changed_files": ["src/a.py"],
            "dependency_map": {"src/a.py": ["tests/a.py"], "docs/x.md": ["tests/docs.py"]},
            "all_tests": ["tests/a.py", "tests/docs.py"],
        }
    )


def test_known_change_is_focused():
    r = M.build_report(payload())
    assert r["selection_mode"] == "FOCUSED" and r["selected_tests"] == ["tests/a.py"]


@pytest.mark.parametrize("path", ["src/unknown.py", "new/file.txt", "README.md"])
def test_unknown_change_selects_full(path):
    v = payload()
    v["changed_files"] = [path]
    seal(v)
    r = M.build_report(v)
    assert r["selection_mode"] == "FULL" and r["selected_tests"] == r["all_tests"]


@pytest.mark.parametrize(
    "path",
    ["pyproject.toml", "requirements-dev.txt", ".github/workflows/x.yml", "scripts/local/x.py"],
)
def test_control_surface_selects_full(path):
    v = payload()
    v["changed_files"] = [path]
    v["dependency_map"][path] = ["tests/a.py"]
    seal(v)
    assert M.build_report(v)["selection_mode"] == "FULL"


def test_invalid_map_test_fails():
    v = payload()
    v["dependency_map"]["src/a.py"] = ["tests/missing.py"]
    seal(v)
    with pytest.raises(ValueError, match="MAP_TEST"):
        M.build_report(v)


@pytest.mark.parametrize("path", ["../x", "/absolute", "src\\x.py"])
def test_unsafe_path_fails(path):
    v = payload()
    v["changed_files"] = [path]
    seal(v)
    with pytest.raises(ValueError, match="PATH"):
        M.build_report(v)


def test_tamper():
    v = payload()
    v["changed_files"] = ["src/z.py"]
    with pytest.raises(ValueError, match="HASH"):
        M.build_report(v)


@pytest.mark.parametrize(
    "field",
    [
        "coverage_weakened",
        "ci_config_changed",
        "production_database_mutated",
        "services_controlled",
        "execution_authorized",
    ],
)
def test_safe(field):
    assert M.build_report(payload())[field] is False


def test_atomic(tmp_path):
    p = tmp_path / "x"
    r = M.build_report(payload())
    M.publish(p, r)
    assert json.loads(p.read_text()) == r
