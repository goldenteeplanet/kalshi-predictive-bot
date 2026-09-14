#!/bin/sh
set -eu
umask 077
finish() {
  result=$?
  trap - EXIT
  printf '{"task_id":"alpha-cloud-ledger-v1-20260914","status":"FINISHED","exit_code":%s,"next_phase":"review retained evidence; never redispatch this attempt"}\n' "$result" > /output/checkpoint.next
  mv /output/checkpoint.next /output/checkpoint.json
  date -u +%FT%TZ > /output/finished-at.txt
  exit "$result"
}
trap finish EXIT
date -u +%FT%TZ > /output/started-at.txt
printf '%s\n' '{"task_id":"alpha-cloud-ledger-v1-20260914","status":"RUNNING","next_phase":"source pins then eleven new ledger tests"}' > /output/checkpoint.json
cd /work
sha256sum -c source.sha256 > /output/source-verification.txt
cat /sys/fs/cgroup/system.slice/alpha-cloud-ledger-v1-20260914.service/memory.max > /output/memory.max
cat /sys/fs/cgroup/system.slice/alpha-cloud-ledger-v1-20260914.service/cpu.max > /output/cpu.max
cd /output
PYTHONPATH=/work PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest -v test_capacity_ledger > /output/tests.log 2>&1
