from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bc_reproducible_build_identity.py"
    spec = importlib.util.spec_from_file_location("phase4bc_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path):
    module = _module()
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    source1 = root / "src" / "verifier.py"
    source2 = root / "src" / "phase4at_sandbox_executor.py"
    lock = root / "requirements.lock"
    test = root / "tests" / "test_guarded.py"
    source1.write_text('SCHEMA = "phase4ba.independent-verification-report.v1"\n')
    source2.write_text('SCHEMA = "phase4at.sandbox-simulation-receipt.v1"\n')
    lock.write_text("pytest==8.3.5 --hash=sha256:fixture\n")
    test.write_text("def test_guarded(): assert True\n")
    return (
        module,
        root,
        [Path("src/verifier.py"), Path("src/phase4at_sandbox_executor.py")],
        [Path("requirements.lock")],
        [Path("tests/test_guarded.py")],
    )


def _build(f, **overrides):
    module, root, sources, locks, tests = f
    return module.build(
        root,
        source_paths=overrides.get("sources", sources),
        dependency_lock_paths=overrides.get("locks", locks),
        test_paths=overrides.get("tests", tests),
        build_options=overrides.get("options", {"optimization": "none", "offline": True}),
    )


def test_identical_inputs_produce_identical_build_identity(tmp_path: Path):
    f = _fixture(tmp_path)
    first, first_proof = _build(f)
    second, second_proof = _build(f, sources=list(reversed(f[2])))
    assert first == second
    assert first_proof == second_proof
    assert first["safety_capability_manifest"]["sandbox_executor_present"] is True
    assert first["binary_published"] is False and first["deployed"] is False
    assert first["artifact_hash"] == f[0]._hash(first)


@pytest.mark.parametrize("target", ["source", "lock", "test", "option"])
def test_each_identity_input_change_changes_build_hash(tmp_path: Path, target: str):
    f = _fixture(tmp_path)
    baseline, _ = _build(f)
    if target == "source":
        (f[1] / f[2][0]).write_text("# changed\n")
        changed, _ = _build(f)
    elif target == "lock":
        (f[1] / f[3][0]).write_text("pytest==9.0.0\n")
        changed, _ = _build(f)
    elif target == "test":
        (f[1] / f[4][0]).write_text("def test_changed(): assert True\n")
        changed, _ = _build(f)
    else:
        changed, _ = _build(f, options={"optimization": "safe"})
    assert changed["artifact_hash"] != baseline["artifact_hash"]


def test_schema_inventory_interpreter_platform_and_tests_are_bound(tmp_path: Path):
    f = _fixture(tmp_path)
    identity, proof = _build(f)
    assert identity["artifact_schema_versions"] == [
        "phase4at.sandbox-simulation-receipt.v1",
        "phase4ba.independent-verification-report.v1",
    ]
    assert identity["interpreter_identity"]["version"]
    assert identity["platform_identity"]["system"]
    assert identity["test_suite_identity"] == f[0].canonical_hash(identity["test_files"])
    assert proof["build_identity_hash"] == identity["artifact_hash"]


def test_symlink_outside_duplicate_missing_and_invalid_options_fail_closed(tmp_path: Path):
    f = _fixture(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("outside")
    with pytest.raises(ValueError, match="SOURCE_OUTSIDE_ROOT"):
        _build(f, sources=[outside])
    with pytest.raises(ValueError, match="SOURCE_DUPLICATE_OR_INVALID"):
        _build(f, sources=[f[2][0], f[2][0]])
    with pytest.raises(ValueError, match="DEPENDENCY_LOCK_MISSING"):
        _build(f, locks=[])
    with pytest.raises(ValueError, match="BUILD_OPTIONS_INVALID"):
        _build(f, options={"nested": {"invalid": True}})
    link = f[1] / "src" / "link.py"
    try:
        link.symlink_to(f[1] / f[2][0])
    except OSError:
        pytest.skip("symlink unavailable")
    with pytest.raises(ValueError, match="SOURCE_SYMLINK_REFUSED"):
        _build(f, sources=[link])


@pytest.mark.parametrize(
    "token", ["systemctl", "KalshiClient(", "writer_lock", "/home/james/kalshi-runtime-src"]
)
def test_prohibited_capabilities_fail_build_identity(tmp_path: Path, token: str):
    f = _fixture(tmp_path)
    (f[1] / f[2][0]).write_text(token)
    with pytest.raises(ValueError, match="PROHIBITED_BUILD_CAPABILITY"):
        _build(f)


def test_real_guarded_verifier_and_sandbox_sources_build_without_publication():
    module = _module()
    root = Path(__file__).parents[1]
    identity, proof = module.build(
        root,
        source_paths=[
            Path("scripts/local/phase4at_sandbox_executor.py"),
            Path("scripts/local/phase4ba_independent_verifier.py"),
        ],
        dependency_lock_paths=[Path("pyproject.toml")],
        test_paths=[
            Path("tests/test_phase4at_sandbox_executor.py"),
            Path("tests/test_phase4ba_independent_verifier.py"),
        ],
        build_options={"offline": True, "binary": False},
    )
    assert identity["binary_published"] is False
    assert proof["deployed"] is False
