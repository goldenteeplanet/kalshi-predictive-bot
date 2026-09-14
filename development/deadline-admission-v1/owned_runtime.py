"""Cloud/POSIX cooperative worker gate and owner-only deadline monitor.

Not a hostile-code sandbox, source authenticator or real-time scheduling proof.
Caller must supply reviewed code under external cgroup/namespace limits.
"""

import ctypes
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import deadline_admission as D


def clock():
    return datetime.now(UTC)


def subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "SUBREAPER_REQUIRED")


def terminate_and_reap_group(child, grace, safety):
    # Keep the unreaped leader PID reserved until the final group SIGKILL,
    # even when it exited before a TERM-resistant descendant.
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    time.sleep(grace)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    code = child.wait(timeout=safety)
    until = time.monotonic() + safety
    descendants = []
    while True:
        try:
            pid, status = os.waitpid(-child.pid, os.WNOHANG)
        except ChildProcessError:
            return code, descendants
        if pid:
            descendants.append(pid)
            continue
        if time.monotonic() >= until:
            raise RuntimeError("OWNED_GROUP_REAP_UNPROVEN")
        time.sleep(0.01)


def persist(path, data):
    raw = D.encode(data)
    if len(raw) > 16384:
        raise ValueError("RECEIPT_BOUND")
    with path.open("xb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_owned(
    command,
    *,
    output,
    cutoff,
    required_seconds,
    maximum_seconds,
    stop_grace_seconds,
    safety_margin_seconds,
    _after_intent=None,
):
    if (
        type(command) is not tuple
        or not 1 <= len(command) <= 16
        or any(type(s) is not str or not 0 < len(s) <= 4096 for s in command)
    ):
        raise ValueError("BOUNDED_ARGV")
    output = Path(output).absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("OUTPUT_CUSTODY")
    output.mkdir()  # Exclusive identity; an uncertain prior attempt never reuses it.
    parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    budget = dict(
        cutoff=cutoff,
        required_seconds=required_seconds,
        maximum_seconds=maximum_seconds,
        stop_grace_seconds=stop_grace_seconds,
        safety_margin_seconds=safety_margin_seconds,
    )
    first_at = clock()
    first = D.effective_runtime(now=first_at, **budget)
    persist(
        output / "intent.json",
        dict(command=command, cutoff=cutoff.isoformat(), plan=first, status="WORKER_GATE_PENDING"),
    )
    if _after_intent:
        _after_intent()
    # This clock is at actual execution, not the controller's earlier submission.
    actual_start = clock()
    plan = D.effective_runtime(now=actual_start, **budget)
    if (
        first["status"] != "TIME_AVAILABLE"
        or actual_start < first_at
        or plan["status"] != "TIME_AVAILABLE"
    ):
        persist(
            output / "result.json",
            dict(status="LATE_START_REFUSED", workload_started=False, plan=plan),
        )
        raise ValueError("LATE_START_REFUSED")
    monotonic_start = time.monotonic()
    monotonic_deadline = monotonic_start + plan["effective_runtime_seconds"]
    work_cutoff = cutoff - timedelta(seconds=stop_grace_seconds + safety_margin_seconds)
    cause = None
    subreaper()
    with (output / "stdout.log").open("xb") as stdout, (output / "stderr.log").open("xb") as stderr:
        entry = (
            sys.executable,
            str(Path(__file__).with_name("worker_entry.py")),
            cutoff.isoformat(),
            str(required_seconds),
            str(plan["effective_runtime_seconds"]),
            str(stop_grace_seconds),
            str(safety_margin_seconds),
            "0",
            *command,
        )
        child = subprocess.Popen(
            entry, cwd=output, stdout=stdout, stderr=stderr, start_new_session=True
        )
        # Capture this process group only; no global process discovery or kill.
        try:
            while True:
                # WNOWAIT observes leader exit without reaping/releasing its PID.
                exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None:
                    break
                if time.monotonic() >= monotonic_deadline or clock() >= work_cutoff:
                    cause = "DEADLINE_STOP"
                    break
                time.sleep(0.05)
        finally:
            code, descendants = terminate_and_reap_group(
                child, stop_grace_seconds, safety_margin_seconds
            )
    result = dict(
        status=cause
        or (
            "DESCENDANTS_TERMINATED"
            if descendants
            else ("COMPLETED" if code == 0 else "WORKLOAD_FAILED")
        ),
        workload_started=True,
        started_at=actual_start.isoformat(),
        finished_at=clock().isoformat(),
        cutoff=cutoff.isoformat(),
        effective_runtime_seconds=plan["effective_runtime_seconds"],
        child_pid=child.pid,
        child_returncode=code,
        reaped_descendant_pids=descendants,
        elapsed_seconds=time.monotonic() - monotonic_start,
        absolute_and_monotonic_monitor=True,
        realtime_scheduling_guaranteed=False,
    )
    persist(output / "result.json", result)
    return result
