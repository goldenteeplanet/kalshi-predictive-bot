"""Exact development fixture controller. No production units or dynamic target names."""

import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import deadline_admission as D

base = Path("/mnt/kalshi-backup-02/alpha-cloud-dev-20260914")
source = base / "worktree/development/deadline-admission-v1"
attempt = base / "attempt-deadline-v1"
output = base / "results-deadline-v1"
raw = (source / "job.input.json").read_bytes()
config = json.loads(raw)
for name, pin in config["sources"].items():
    assert hashlib.sha256((source / name).read_bytes()).hexdigest() == pin
cutoff = datetime.fromisoformat(config["cutoff"])
def clock():
    return datetime.now(UTC)


def call(argv):
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=10)


def submit(intent):
    # The independent absolute unit cutoff is armed BEFORE substantive work.
    guard = call(
        [
            "systemd-run",
            "--unit=alpha-cloud-deadline-cutoff-v1-20260914",
            "--property=RemainAfterExit=yes",
            "--property=MemoryMax=64M",
            "--property=CPUQuota=10%",
            "--property=TasksMax=8",
            "--property=RuntimeMaxSec=600",
            "/usr/bin/python3",
            str(source / "absolute_cutoff.py"),
            config["cutoff"],
        ]
    )
    until = time.monotonic() + 3
    while not (attempt / "watchdog.ready").exists():
        if time.monotonic() > until:
            raise RuntimeError("ABSOLUTE_WATCHDOG_NOT_READY")
        time.sleep(0.05)
    if (attempt / "watchdog.ready").read_text() != config["cutoff"]:
        raise RuntimeError("WATCHDOG_CUTOFF_MISMATCH")
    actual = D.effective_runtime(clock(), cutoff, 20, 60, 2, 5)
    if actual["status"] != "TIME_AVAILABLE":
        raise RuntimeError("DELAYED_SUBMISSION_REFUSED")
    D.save(
        attempt,
        "actual-submission.json",
        dict(
            at=clock().isoformat(),
            cutoff=config["cutoff"],
            effective_runtime_seconds=actual["effective_runtime_seconds"],
            guard_stdout=guard.stdout,
            guard_stderr=guard.stderr,
        ),
    )
    properties = [
        "Type=exec",
        "ExitType=main",
        "RemainAfterExit=no",
        "SendSIGKILL=yes",
        "User=nobody",
        "Group=nogroup",
        "WorkingDirectory=/output",
        "Environment=PYTHONPATH=/work PYTHONDONTWRITEBYTECODE=1",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateNetwork=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "NoNewPrivileges=yes",
        "TemporaryFileSystem=/mnt:ro",
        f"BindReadOnlyPaths={source}:/work",
        f"BindPaths={output}:/output",
        "InaccessiblePaths=/opt /root /home /var/lib /var/log",
        "MemoryMax=256M",
        "MemorySwapMax=0",
        "CPUQuota=25%",
        "TasksMax=16",
        "Nice=19",
        "IOWeight=10",
        f"RuntimeMaxSec={actual['effective_runtime_seconds']}",
        "TimeoutStopSec=2",
        "KillMode=control-group",
        "Restart=no",
    ]
    launched = call(
        [
            "systemd-run",
            "--unit=alpha-cloud-deadline-v1-20260914",
            *[f"--property={p}" for p in properties],
            "/usr/bin/python3",
            "/work/integrated_suite.py",
        ]
    )
    D.save(
        attempt,
        "daemon-returned.json",
        dict(stdout=launched.stdout, stderr=launched.stderr, at=clock().isoformat()),
    )
    effective = call(
        [
            "systemctl",
            "show",
            "alpha-cloud-deadline-v1-20260914.service",
            "-p",
            "Type,ExitType,RemainAfterExit,KillMode,SendSIGKILL,TimeoutStopUSec,RuntimeMaxUSec,ControlGroup,MemoryMax,CPUQuotaPerSecUSec,TasksMax,PrivateNetwork,ProtectSystem,MainPID,ExecMainStartTimestamp",
        ]
    )
    D.save(
        attempt, "effective-unit.json", dict(properties=effective.stdout, at=clock().isoformat())
    )
    # Deliberate NEW integrated uncertain-ack fixture after actual submission.
    raise RuntimeError("SIMULATED_ACK_LOSS_AFTER_ACTUAL_SYSTEMD_SUBMISSION")


result = D.dispatch_once(
    attempt,
    identity="dispatch-once",
    source_sha256=config["sources"]["deadline_admission.py"],
    input_raw=raw,
    input_sha256=D.sha(raw),
    declared_at=datetime.fromisoformat(config["declared_at"]),
    not_before=datetime.fromisoformat(config["not_before"]),
    cutoff=cutoff,
    required_work_seconds=20,
    maximum_runtime_seconds=60,
    stop_grace_seconds=2,
    safety_seconds=5,
    clock=clock,
    submit=submit,
)
print(result)
