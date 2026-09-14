"""Actual child entry gate immediately before exec of reviewed workload."""

import os
import sys
import time
from datetime import UTC, datetime

import deadline_admission as D

cutoff = datetime.fromisoformat(sys.argv[1])
required, maximum, grace, safety = map(int, sys.argv[2:6])
delay = float(sys.argv[6])
assert 0 <= delay <= 5
if delay:
    time.sleep(delay)  # Explicit synthetic delayed-entry fixture only.
plan = D.effective_runtime(datetime.now(UTC), cutoff, required, maximum, grace, safety)
if plan["status"] != "TIME_AVAILABLE":
    sys.exit(78)
command = sys.argv[7:]
assert command and command[0].startswith("/")
os.execv(command[0], command)
