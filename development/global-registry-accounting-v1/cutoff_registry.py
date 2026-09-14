"""Privileged controller for ONE fixed owned development unit, never production."""

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

root = Path("/mnt/kalshi-backup-02/alpha-cloud-dev-20260914/attempt-registry-accounting-v1")
cutoff = datetime.fromisoformat(sys.argv[1])
assert cutoff.tzinfo is not None
unit = "alpha-cloud-registry-accounting-v1-20260914.service"
remaining = (cutoff - datetime.now(UTC)).total_seconds()
assert 0 < remaining <= 600
mono = time.monotonic() + remaining
(root / "watchdog.ready").write_text(cutoff.isoformat())
while time.monotonic() < mono and datetime.now(UTC) < cutoff:
    time.sleep(0.1)
# Kill the entire exact owned unit, even if its leader is already gone.
r = subprocess.run(
    ["systemctl", "kill", "--kill-whom=all", "--signal=SIGKILL", unit],
    capture_output=True,
    text=True,
    timeout=5,
)
(root / "watchdog-result.json").write_text(
    json.dumps(
        dict(
            at=datetime.now(UTC).isoformat(),
            unit=unit,
            returncode=r.returncode,
            stdout=r.stdout,
            stderr=r.stderr,
        )
    )
)
