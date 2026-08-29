from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.local.phase4lj_workstream_certification import PhaseSpec, certify


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgSign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(tmp_path: Path):
    _git(tmp_path, "init", "-q", "-b", "certify")
    _git(tmp_path, "config", "user.email", "phase4lj@example.invalid")
    _git(tmp_path, "config", "user.name", "Phase 4LJ")
    specs = []
    for phase, subject, path in (
        ("4KX", "phase4kx: one", "one.txt"),
        ("4KY", "phase4ky: two", "two.txt"),
    ):
        (tmp_path / path).write_text(phase, encoding="utf-8")
        _git(tmp_path, "add", path)
        _git(tmp_path, "commit", "-qm", subject)
        specs.append(PhaseSpec(phase, subject, (path,)))
    tests = {phase.phase: {"passed": 1, "failed": 0, "skipped": 0} for phase in specs}
    runtime = {
        "wsl_responsive": True,
        "scheduler_active": True,
        "ui_active": True,
        "live_disabled": True,
        "demo_disabled": True,
        "autopilot_disabled": True,
        "paper_creation_disabled": True,
        "paper_kill_switch_enabled": True,
        "no_additional_order_created": True,
    }
    return tuple(specs), tests, runtime


def test_valid_workstream_passes_deterministically(tmp_path: Path) -> None:
    specs, tests, runtime = _fixture(tmp_path)
    first = certify(tmp_path, tests, runtime, specs, ())
    second = certify(tmp_path, tests, runtime, specs, ())
    assert first == second
    assert first["verdict"] == "PASS"


def test_missing_or_dirty_deliverable_refuses(tmp_path: Path) -> None:
    specs, tests, runtime = _fixture(tmp_path)
    (tmp_path / "two.txt").write_text("dirty", encoding="utf-8")
    assert "4KY:PHASE_FILES_DIRTY" in certify(tmp_path, tests, runtime, specs, ())["errors"]


def test_test_failure_or_skip_refuses(tmp_path: Path) -> None:
    specs, tests, runtime = _fixture(tmp_path)
    tests["4KY"] = {"passed": 1, "failed": 0, "skipped": 1}
    assert "4KY:TEST_GATE_FAILED" in certify(tmp_path, tests, runtime, specs, ())["errors"]


def test_runtime_invariant_failure_refuses(tmp_path: Path) -> None:
    specs, tests, runtime = _fixture(tmp_path)
    runtime["paper_kill_switch_enabled"] = False
    errors = certify(tmp_path, tests, runtime, specs, ())["errors"]
    assert "RUNTIME_GATE_FAILED:paper_kill_switch_enabled" in errors


def test_wrong_commit_payload_refuses(tmp_path: Path) -> None:
    specs, tests, runtime = _fixture(tmp_path)
    wrong = (PhaseSpec("4KX", specs[0].subject, ("different.txt",)), specs[1])
    assert "4KX:COMMIT_PATH_MISMATCH" in certify(tmp_path, tests, runtime, wrong, ())["errors"]


def test_duplicate_or_missing_subject_refuses(tmp_path: Path) -> None:
    specs, tests, runtime = _fixture(tmp_path)
    missing = (PhaseSpec("4KX", "phase4kx: missing", ("one.txt",)), specs[1])
    assert "4KX:COMMIT_SUBJECT_COUNT:0" in certify(tmp_path, tests, runtime, missing, ())["errors"]


def test_certificate_explicitly_grants_no_authority(tmp_path: Path) -> None:
    specs, tests, runtime = _fixture(tmp_path)
    result = certify(tmp_path, tests, runtime, specs, ())
    assert all(value is False for value in result["safety"].values())
    assert "grants no" in result["residual_risks"][-1]
