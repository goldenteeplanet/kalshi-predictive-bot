import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
S = ROOT / "scripts/local/phase4fi_reproducible_ci.py"
X = importlib.util.spec_from_file_location("m", S)
assert X and X.loader
M = importlib.util.module_from_spec(X)
X.loader.exec_module(M)


def fixture(tmp_path):
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "requirements-ci.lock").write_text("pytest==8.2.0\nruff==0.5.0\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / ".github/workflows/phase4-required-safety.yml").write_text(
        'python-version: "3.11.9"\nuses: actions/checkout@'
        + "a" * 40
        + "\nrun: pip install -c requirements-ci.lock -e .\n"
    )
    return tmp_path


def test_real_ci_is_reproducibly_identified():
    r = M.audit(ROOT, "a" * 64)
    assert r["python"] == "3.11.9" and len(r["environment_identity"]) == 64


def test_fixture_is_deterministic(tmp_path):
    root = fixture(tmp_path)
    assert (
        M.audit(root, "a" * 64)["environment_identity"]
        == M.audit(root, "a" * 64)["environment_identity"]
    )


@pytest.mark.parametrize("line", ["pytest>=8", "pytest", "pytest = 8", "pytest==8.2 pytest"])
def test_unpinned_dependency_fails(tmp_path, line):
    root = fixture(tmp_path)
    (root / "requirements-ci.lock").write_text(line + "\n")
    with pytest.raises(ValueError, match="EXACTLY_PINNED"):
        M.audit(root, "a" * 64)


def test_duplicate_dependency_fails(tmp_path):
    root = fixture(tmp_path)
    (root / "requirements-ci.lock").write_text("pytest==8.2.0\npytest==8.2.0\n")
    with pytest.raises(ValueError, match="EXACTLY_PINNED"):
        M.audit(root, "a" * 64)


def test_wrong_python_fails(tmp_path):
    root = fixture(tmp_path)
    p = root / ".github/workflows/phase4-required-safety.yml"
    p.write_text(p.read_text().replace("3.11.9", "3.12"))
    with pytest.raises(ValueError, match="WORKFLOW_NOT_BOUND"):
        M.audit(root, "a" * 64)


def test_floating_action_fails(tmp_path):
    root = fixture(tmp_path)
    p = root / ".github/workflows/phase4-required-safety.yml"
    p.write_text(p.read_text().replace("a" * 40, "v4"))
    with pytest.raises(ValueError, match="ACTION_NOT_PINNED"):
        M.audit(root, "a" * 64)


@pytest.mark.parametrize(
    "field",
    [
        "remote_ci_executed",
        "dependencies_installed",
        "files_changed_by_audit",
        "production_database_mutated",
        "services_controlled",
        "execution_authorized",
    ],
)
def test_audit_is_read_only(field, tmp_path):
    assert M.audit(fixture(tmp_path), "a" * 64)[field] is False
