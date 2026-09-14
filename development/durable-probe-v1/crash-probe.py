"""New cloud-owned long checkpoint/abrupt child exit fixture; synthetic only."""

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import capacity_ledger as L


def now():
    return datetime.now(UTC).isoformat()


policy = L.encode(
    dict(
        filesystem="synthetic-fs",
        epoch="synthetic-epoch",
        observer_source_sha256="a" * 64,
        source_manifest_sha256="c" * 64,
        max_age_seconds=2,
        protected_margin_bytes=0,
        max_rows=4,
        max_evidence_bytes=8192,
        max_database_bytes=1048576,
        lock_timeout_ms=1000,
        transaction_seconds=3,
    )
)
policy_pin = L.sha(policy)
observation = L.encode(
    dict(
        schema="FREE_SPACE_OBSERVATION_V1",
        filesystem="synthetic-fs",
        available_bytes=L.P.PAGE_PHASE_BYTES,
        sample_start="2026-09-14T15:00:00Z",
        sample_end="2026-09-14T15:00:00.1Z",
        elapsed_ms=100,
        observer_source_sha256="a" * 64,
        raw_evidence_sha256="b" * 64,
    )
)
request = L.encode(
    dict(
        reservation="abrupt-child-once",
        run="owned-child",
        phase="synthetic",
        filesystem="synthetic-fs",
        epoch="synthetic-epoch",
        source_manifest_sha256="c" * 64,
        policy_sha256=policy_pin,
        expected_generation=0,
        pages=1,
        phases=1,
        decision_at="2026-09-14T15:00:01Z",
        observation_sha256=L.sha(observation),
    )
)
db = Path("/output/abrupt-child.sqlite")
if len(sys.argv) == 2 and sys.argv[1] == "child":

    def abrupt(phase):
        if phase == "after_commit_before_ack":
            os._exit(73)

    L.reserve(
        db,
        policy_raw=policy,
        policy_sha256=policy_pin,
        request_raw=request,
        observation_raw=observation,
        _checkpoint=abrupt,
    )
    raise RuntimeError("EXPECTED_ABRUPT_EXIT")

started = time.monotonic()
previous = "0" * 64


def checkpoint(phase, status="RUNNING", **details):
    global previous
    record = dict(
        task_id="alpha-cloud-durable-probe-v1-20260914",
        at=now(),
        phase=phase,
        status=status,
        previous_sha256=previous,
        elapsed_seconds=time.monotonic() - started,
        **details,
    )
    raw = L.encode(record)
    previous = L.sha(raw)
    with Path("/output/progress.jsonl").open("ab") as f:
        f.write(raw + b"\n")
        f.flush()
        os.fsync(f.fileno())
    temporary = Path("/output/checkpoint.next")
    with temporary.open("wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    temporary.replace("/output/checkpoint.json")


checkpoint(
    "CREATE_SYNTHETIC_LEDGER", next_phase="owned child commits then exits without acknowledgement"
)
L.create(db, policy_raw=policy, policy_sha256=policy_pin)
child = subprocess.run(
    [sys.executable, "/work/crash-probe.py", "child"], capture_output=True, timeout=15
)
Path("/output/child.stdout").write_bytes(child.stdout)
Path("/output/child.stderr").write_bytes(child.stderr)
assert child.returncode == 73, child.returncode
digest = L.sha(L.encode(dict(request_sha256=L.sha(request), observation_sha256=L.sha(observation))))
reconciled = L.reconcile(
    db,
    policy_raw=policy,
    policy_sha256=policy_pin,
    reservation="abrupt-child-once",
    input_sha256=digest,
)
assert reconciled["status"] == "EXISTING_RECEIPT_READ_ONLY" and reconciled["generation"] == 1
assert not reconciled["launch_authorized"] and not reconciled["redispatch_authorized"]
Path("/output/original-reservation-receipt.json").write_bytes(reconciled["receipt"])
try:
    L.reserve(
        db,
        policy_raw=policy,
        policy_sha256=policy_pin,
        request_raw=request,
        observation_raw=observation,
    )
except ValueError as exc:
    assert str(exc) == "DUPLICATE_RESERVATION"
else:
    raise AssertionError("DUPLICATE_DISPATCH_ADMITTED")
checkpoint(
    "ABRUPT_CHILD_RECONCILED",
    child_exit=child.returncode,
    receipt_sha256=L.sha(reconciled["receipt"]),
    redispatch_authorized=False,
    next_phase="seven30second cloud-owned checkpoint intervals",
)
for interval in range(1, 8):
    time.sleep(30)
    checkpoint("CLOUD_INTERVAL_" + str(interval), next_phase="next interval or final receipt")
assert time.monotonic() - started >= 210
checkpoint(
    "COMPLETE",
    "PASS_SCOPED_DURABLE_JOB_FIXTURE",
    child_exit=73,
    redispatch_authorized=False,
    browser_reboot_tested=False,
    next_phase="independent evidence review; do not repeat this identity",
)
