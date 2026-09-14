#!/bin/sh
set -eu
umask 077
date -u +%FT%TZ > /output/started-at.txt
test ! -r /opt/kalshi-predictive-bot/pyproject.toml
test ! -e /mnt/kalshi-backup-02/alpha-crash-follow-on-fresh-fee-v2-20260914
test -r /work/capacity_plan.py
if touch /work/UNAUTHORIZED_WRITE 2>/output/readonly-refusal.txt; then exit 91; fi
cat /proc/self/cgroup > /output/cgroup.txt
cat /sys/fs/cgroup/system.slice/alpha-cloud-isolation-v1-20260914.service/memory.max > /output/memory.max
cat /sys/fs/cgroup/system.slice/alpha-cloud-isolation-v1-20260914.service/memory.swap.max > /output/memory.swap.max
cat /sys/fs/cgroup/system.slice/alpha-cloud-isolation-v1-20260914.service/cpu.max > /output/cpu.max
cat /sys/fs/cgroup/system.slice/alpha-cloud-isolation-v1-20260914.service/pids.max > /output/pids.max
cat /proc/net/dev > /output/network-devices.txt
sleep 5
date -u +%FT%TZ > /output/continued-after-launch-ssh.txt
printf '%s\n' '{"task_id":"alpha-cloud-isolation-v1-20260914","status":"PASS_SCOPED_ISOLATION_FIXTURE","production_readable":false,"candidate_visible":false,"source_writable":false,"next_phase":"inspect effective unit and cgroup receipts"}' > /output/checkpoint.json
