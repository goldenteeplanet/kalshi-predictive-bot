"""Native incarnation evidence from live and exited real child processes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kalshi_predictor.overnight_paper.runtime_liveness import (
    current_process_start_identity,
    inspect_process,
)


def test_current_process_and_wrong_incarnation():
    identity = current_process_start_identity()
    if identity is None:
        assert inspect_process(os.getpid(), "unknown").state == "UNVERIFIED"
        return
    assert inspect_process(os.getpid(), identity).state == "RUNNING"
    assert inspect_process(os.getpid(), identity + "different").state == "STOPPED"
    assert inspect_process(os.getpid(), None).state == "UNVERIFIED"


@pytest.mark.parametrize("pid", [-1, 0, True, 2**40])
def test_invalid_pid_is_unknown(pid):
    assert inspect_process(pid, "not-native").state == "UNVERIFIED"


def test_real_child_start_identity_and_exit():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys,os; "
            "from kalshi_predictor.overnight_paper.runtime_liveness import "
            "current_process_start_identity; "
            "identity=current_process_start_identity() or 'UNKNOWN'; "
            "print(str(os.getpid())+' '+identity,flush=True); "
            "sys.stdin.readline()",
        ],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        pid_text, identity = child.stdout.readline().strip().split(" ", 1)
        native_pid = int(pid_text)
        assert identity
        if identity == "UNKNOWN":
            assert inspect_process(native_pid, identity).state == "UNVERIFIED"
        else:
            assert inspect_process(native_pid, identity).state == "RUNNING"
            # A reused PID with a different start identity cannot appear alive.
            assert inspect_process(native_pid, identity + "old-generation").state == "STOPPED"
        _, error = child.communicate("exit\n", timeout=15)
        assert child.returncode == 0, error
        if identity != "UNKNOWN":
            assert inspect_process(native_pid, identity).state == "STOPPED"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=15)
