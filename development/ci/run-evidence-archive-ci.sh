#!/usr/bin/env bash
# Only ten archive cases; no historical dispatcher or cloud fixture invocation.
set -Eeuo pipefail
repo=$(cd "${1:?repository path required}" && pwd)
output=${2:?fresh evidence directory required}
mkdir "$output"
output=$(cd "$output" && pwd)
mkdir "$output/archives"
unit="alpha-ci-evidence-archive-v1-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}.service"
submitted=0
cleanup() {
  if [[ "$submitted" == 1 ]]; then
    local stop_status=0
    sudo systemctl stop "$unit" > "$output/stop.log" 2>&1 || stop_status=$?
    printf 'stop_exit=%s\n' "$stop_status" > "$output/final-cleanup.result"
    if [[ -e "/sys/fs/cgroup/system.slice/$unit" ]]; then
      printf 'FAIL: owned cgroup still present\n' >> "$output/final-cleanup.result"
      return 1
    fi
    printf 'PASS: owned cgroup absent\n' >> "$output/final-cleanup.result"
    sudo systemctl reset-failed "$unit" >/dev/null 2>&1 || true
  fi
}
trap 'status=$?; cleanup || status=1; exit "$status"' EXIT
source_dir="$repo/development/evidence-archive-v1"
printf '%s  %s\n' \
  e8452e389a4740e693257b6a8913a053df581fcf033489452d13400eb2f224e0 "$source_dir/evidence_archive.py" \
  4c577d0a512974335d7b6640a0c68d463fe00534b84d63f430bf448eeb777a7d "$source_dir/test_evidence_archive.py" > "$output/source.properties"
sha256sum --check "$output/source.properties" > "$output/source-check.result"
sudo systemctl show "$unit" -p LoadState > "$output/preflight.properties"
grep -Fxq 'LoadState=not-found' "$output/preflight.properties"
[[ ! -e "/sys/fs/cgroup/system.slice/$unit" ]]
# Mark the attempt before I/O; uncertainty never authorizes retry.
submitted=1
(
# Bound the forwarding client's regular log file as well as unit-owned files.
ulimit -f 1024
sudo systemd-run --unit="$unit" --wait --pipe \
  --property=Type=exec --property=ExitType=main --property=RemainAfterExit=no \
  --property=KillMode=control-group --property=SendSIGKILL=yes \
  --property=TimeoutStopSec=2s --property=RuntimeMaxSec=90s \
  --property=MemoryMax=512M --property=CPUQuota=50% --property=TasksMax=32 \
  --property=LimitFSIZE=1048576 \
  --property="User=$(id -u)" --property="Group=$(id -g)" \
  --property=PrivateNetwork=yes --property=NoNewPrivileges=yes \
  --property=ProtectSystem=strict --property=PrivateTmp=yes \
  --property="BindReadOnlyPaths=$source_dir:/work" \
  --property="BindPaths=$output:/output" --property=WorkingDirectory=/output \
  --setenv=PYTHONPATH=/work --setenv=PYTHONDONTWRITEBYTECODE=1 \
  --setenv=EVIDENCE_TEST_ROOT=/output/archives \
  /bin/bash -euc '
    /usr/bin/systemctl show "$1" -p Type -p ExitType -p RemainAfterExit -p KillMode -p SendSIGKILL -p TimeoutStopUSec -p RuntimeMaxUSec -p MemoryMax -p CPUQuotaPerSecUSec -p TasksMax -p LimitFSIZE -p LimitFSIZESoft -p PrivateNetwork -p NoNewPrivileges -p ProtectSystem -p PrivateTmp > /output/effective.properties
    for expected in Type=exec ExitType=main RemainAfterExit=no KillMode=control-group SendSIGKILL=yes "TimeoutStopUSec=2s" "RuntimeMaxUSec=1min 30s" MemoryMax=536870912 CPUQuotaPerSecUSec=500ms TasksMax=32 LimitFSIZE=1048576 LimitFSIZESoft=1048576 PrivateNetwork=yes NoNewPrivileges=yes ProtectSystem=strict PrivateTmp=yes; do
      grep -Fxq "$expected" /output/effective.properties
    done
    /usr/bin/python3 -c "import sys,unittest; suite=unittest.defaultTestLoader.discover(\"/work\",pattern=\"test_evidence_archive.py\"); assert suite.countTestCases()==10, suite.countTestCases(); result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(0 if result.wasSuccessful() else 1)"
    printf "PASS: exactly ten archive cases\n" > /output/tests.result
  ' archive-ci-worker "$unit"
) > "$output/unit.log" 2>&1
[[ $(cat "$output/tests.result") == 'PASS: exactly ten archive cases' ]]
[[ ! -e "/sys/fs/cgroup/system.slice/$unit" ]]
printf 'PASS: whole owned cgroup absent after unit completion\n' > "$output/cleanup.result"
# EXIT trap stops only this unit and records cleanup; archives are never deleted.
