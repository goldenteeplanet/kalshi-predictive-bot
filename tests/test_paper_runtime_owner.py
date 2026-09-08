"""Real OS contention and restart without SQLite mutation or polling sleeps."""

import os
import pickle
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from kalshi_predictor.overnight_paper import runtime_owner
from kalshi_predictor.overnight_paper.runtime_owner import (
    acquire_runtime_owner,
    validate_runtime_owner,
)


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "paper.sqlite3"
    path.write_bytes(b"existing ledger sentinel; no SQLite access required")
    return path


def test_owner_identity_lifetime_and_stale_sidecar_restart(ledger):
    original = ledger.read_bytes()
    with acquire_runtime_owner(ledger) as first:
        validate_runtime_owner(first, ledger)
        with pytest.raises(ValueError, match="ALREADY_ACTIVE"):
            with acquire_runtime_owner(ledger):
                pytest.fail("duplicate owner")
        with pytest.raises(ValueError, match="NOT_ACTIVE"):
            validate_runtime_owner(replace(first), ledger)
        with pytest.raises(ValueError, match="PROCESS_MISMATCH"):
            validate_runtime_owner(replace(first, pid=first.pid + 1), ledger)
        with pytest.raises(ValueError, match="PROCESS_START_IDENTITY_MISMATCH"):
            validate_runtime_owner(replace(first, process_start_identity="wrong-start"), ledger)
        with pytest.raises(TypeError, match="NOT_SERIALIZABLE"):
            pickle.dumps(first)
    with pytest.raises(ValueError, match="NOT_ACTIVE"):
        validate_runtime_owner(first, ledger)
    lock = ledger.with_name(ledger.name + ".runtime-owner.lock")
    identity = (lock.stat().st_dev, lock.stat().st_ino)
    with acquire_runtime_owner(ledger) as second:
        assert second.generation != first.generation
        assert (lock.stat().st_dev, lock.stat().st_ino) == identity
    assert ledger.read_bytes() == original


def test_missing_and_onedrive_paths_rejected(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        with acquire_runtime_owner(path):
            pytest.fail("created database")
    assert not path.exists()
    directory = tmp_path / "OneDrive"
    directory.mkdir()
    path = directory / "paper.sqlite3"
    path.write_bytes(b"original")
    with pytest.raises(ValueError, match="ONEDRIVE"):
        with acquire_runtime_owner(path):
            pytest.fail("OneDrive accepted")


def test_wrong_and_replaced_ledger_rejected(ledger, tmp_path):
    other = tmp_path / "other.sqlite3"
    other.write_bytes(b"other")
    with acquire_runtime_owner(ledger) as owner:
        with pytest.raises(ValueError, match="NOT_ACTIVE"):
            validate_runtime_owner(owner, other)
        replacement = tmp_path / "replacement.sqlite3"
        replacement.write_bytes(b"replacement")
        os.replace(replacement, ledger)
        with pytest.raises(ValueError, match="DATABASE_IDENTITY_CHANGED"):
            validate_runtime_owner(owner, ledger)


def test_hard_link_rejected(ledger, tmp_path):
    alias = tmp_path / "alias.sqlite3"
    os.link(ledger, alias)
    with pytest.raises(ValueError, match="UNLINKED_DATABASE"):
        with acquire_runtime_owner(alias):
            pytest.fail("hard link accepted")


def test_closed_descriptor_revokes_owner(ledger):
    manager = acquire_runtime_owner(ledger)
    owner = manager.__enter__()
    os.close(runtime_owner._ACTIVE[owner.database_path].descriptor)
    with pytest.raises(ValueError, match="DESCRIPTOR_NOT_HELD"):
        validate_runtime_owner(owner, ledger)
    with pytest.raises(OSError):
        manager.__exit__(None, None, None)
    with pytest.raises(ValueError, match="NOT_ACTIVE"):
        validate_runtime_owner(owner, ledger)
    with acquire_runtime_owner(ledger) as restarted:
        validate_runtime_owner(restarted, ledger)


def test_linked_sidecar_rejected(ledger, tmp_path):
    original = tmp_path / "original.lock"
    original.write_bytes(b"0")
    os.link(original, ledger.with_name(ledger.name + ".runtime-owner.lock"))
    with pytest.raises(ValueError, match="UNLINKED_LOCK"):
        with acquire_runtime_owner(ledger):
            pytest.fail("linked lock accepted")


@pytest.mark.parametrize("terminate", [False, True])
def test_real_subprocess_exclusion_and_release(ledger, terminate):
    code = (
        "import sys,os; from pathlib import Path; "
        "from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner; "
        "manager=acquire_runtime_owner(Path(sys.argv[1])); owner=manager.__enter__(); "
        "print(str(os.getpid())+' '+owner.generation,flush=True); sys.stdin.readline(); "
        "manager.__exit__(None,None,None)"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(ledger)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert child.stdout is not None
        pid_text, generation = child.stdout.readline().strip().split(" ", 1)
        assert len(generation) == 32
        with pytest.raises(ValueError, match="LOCK_CONTENDED"):
            with acquire_runtime_owner(ledger):
                pytest.fail("cross-process lock bypass")
        if terminate:
            os.kill(int(pid_text), signal.SIGTERM)
            child.communicate(timeout=15)
        else:
            _, errors = child.communicate("release\n", timeout=15)
            assert child.returncode == 0, errors
        with acquire_runtime_owner(ledger) as next_owner:
            validate_runtime_owner(next_owner, ledger)
            assert next_owner.generation != generation
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=15)
