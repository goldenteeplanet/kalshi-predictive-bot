"""Pinned registry-only test worker inside one bounded development cgroup."""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import deadline_admission as D
import owned_runtime as R

config = json.loads(Path('/work/job.input.json').read_bytes())
for name, pin in config['sources'].items():
    assert hashlib.sha256((Path('/work') / name).read_bytes()).hexdigest() == pin
assert datetime.now(UTC) >= datetime.fromisoformat(config['not_before'])
unit = 'alpha-cloud-registry-accounting-v1-20260914.service'
receipt = subprocess.run(
    ['systemctl', 'show', unit, '-p',
     'Type,ExitType,RemainAfterExit,KillMode,SendSIGKILL,TimeoutStopUSec,'
     'RuntimeMaxUSec,MemoryMax,CPUQuotaPerSecUSec,TasksMax,PrivateNetwork,ProtectSystem'],
    capture_output=True, text=True, check=True, timeout=5,
)
properties = dict(line.split('=', 1) for line in receipt.stdout.splitlines() if '=' in line)
expected = {
    'Type': 'exec', 'ExitType': 'main', 'RemainAfterExit': 'no',
    'KillMode': 'control-group', 'SendSIGKILL': 'yes', 'TimeoutStopUSec': '2s',
    'MemoryMax': '268435456', 'CPUQuotaPerSecUSec': '250ms', 'TasksMax': '16',
    'PrivateNetwork': 'yes', 'ProtectSystem': 'strict',
}
assert all(properties.get(key) == value for key, value in expected.items()), properties
Path('/output/worker-effective.json').write_bytes(D.encode(properties))
result = R.run_owned(
    (sys.executable, '/work/run_registry_tests.py'),
    output=Path('/output/suite'),
    cutoff=datetime.fromisoformat(config['cutoff']),
    required_seconds=20, maximum_seconds=60,
    stop_grace_seconds=2, safety_margin_seconds=5,
)
Path('/output/job-result.json').write_bytes(D.encode(result))
sys.exit(0 if result['status'] == 'COMPLETED' else 1)
