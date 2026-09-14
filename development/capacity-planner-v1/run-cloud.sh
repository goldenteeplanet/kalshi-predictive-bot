#!/bin/sh
set -eu
umask 077
cd /work
printf '%s\n' '{"task_id":"alpha-cloud-planner-v1-20260914","status":"RUNNING","next_phase":"disconnect marker then eight new tests","source_sha":"b3f233b37b13433ae8bee010dcf8d05dfe630614d24caa392556926c12868c8a"}' > /output/checkpoint.json
date -u +%FT%TZ > /output/started-at.txt
sha256sum capacity_plan.py test_capacity_plan.py run-cloud.sh > /output/source-sha256.txt
sleep 5
date -u +%FT%TZ > /output/continued-after-launch-ssh.txt
set +e
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest -v test_capacity_plan > /output/tests.log 2>&1
result=$?
set -e
printf '{"task_id":"alpha-cloud-planner-v1-20260914","status":"FINISHED","exit_code":%s,"next_phase":"independent review; no production authority","source_sha":"b3f233b37b13433ae8bee010dcf8d05dfe630614d24caa392556926c12868c8a"}\n' "$result" > /output/checkpoint.next
mv /output/checkpoint.next /output/checkpoint.json
date -u +%FT%TZ > /output/finished-at.txt
exit "$result"
