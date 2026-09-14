"""Read-only terminal evidence audit, not a rerun of the probe."""

import hashlib
import json
import signal
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

signal.alarm(8)
root = Path(sys.argv[1])
for name, limit in [
    ("progress.jsonl", 65536),
    ("checkpoint.json", 16384),
    ("original-reservation-receipt.json", 16384),
    ("abrupt-child.sqlite", 1048576),
]:
    assert not (root / name).is_symlink() and (root / name).stat().st_size <= limit
raw = (root / "progress.jsonl").read_bytes()
assert len(raw) < 65536 and raw.endswith(b"\n")
lines = raw.splitlines()
assert len(lines) == 10
previous = "0" * 64
prior_elapsed = -1
prior_time = None
records = []
for line in lines:
    record = json.loads(line)
    assert record["task_id"] == "alpha-cloud-durable-probe-v1-20260914"
    assert record["previous_sha256"] == previous
    assert record["elapsed_seconds"] >= prior_elapsed
    at = datetime.fromisoformat(record["at"])
    assert at.utcoffset().total_seconds() == 0
    assert prior_time is None or at >= prior_time
    previous = hashlib.sha256(line).hexdigest()
    prior_elapsed = record["elapsed_seconds"]
    prior_time = at
    records.append(record)
assert [r["phase"] for r in records] == ["CREATE_SYNTHETIC_LEDGER", "ABRUPT_CHILD_RECONCILED"] + [
    "CLOUD_INTERVAL_" + str(i) for i in range(1, 8)
] + ["COMPLETE"]
assert (root / "checkpoint.json").read_bytes() == lines[-1]
assert (
    records[-1]["status"] == "PASS_SCOPED_DURABLE_JOB_FIXTURE"
    and records[-1]["elapsed_seconds"] >= 210
)
assert records[1]["child_exit"] == records[-1]["child_exit"] == 73
assert records[1]["redispatch_authorized"] is records[-1]["redispatch_authorized"] is False
receipt = (root / "original-reservation-receipt.json").read_bytes()
assert hashlib.sha256(receipt).hexdigest() == records[1]["receipt_sha256"]
with sqlite3.connect((root / "abrupt-child.sqlite").as_uri() + "?mode=ro", uri=True) as db:
    db.execute("PRAGMA query_only=ON")
    assert db.execute("SELECT generation FROM head").fetchall() == [(1,)]
    rows = db.execute("SELECT id,receipt,bytes,state FROM reservations").fetchall()
    assert rows == [("abrupt-child-once", receipt, 536870912, "RESERVED")]
print(
    json.dumps(
        dict(
            at=datetime.now(UTC).isoformat(),
            status="TERMINAL_CHAIN_AND_LEDGER_READBACK_PASS",
            records=10,
            elapsed_seconds=records[-1]["elapsed_seconds"],
            child_exit=73,
            retained_charge_bytes=536870912,
            last_checkpoint_sha256=previous,
            power_loss_proven=False,
            windows_reboot_proven=False,
        )
    )
)
