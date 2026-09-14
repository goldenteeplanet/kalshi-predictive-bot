#!/usr/bin/env bash
# Execute only development unit suites. Historical cloud dispatchers are never run.
set -Eeuo pipefail
repo=$(cd "${1:?repository path required}" && pwd)
evidence=${2:?evidence directory required}
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
unit=''
cleanup() {
  if [[ -n "$unit" ]]; then
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
for suite in capacity-planner-v1 capacity-ledger-v1 deadline-admission-v1; do
  output="$evidence/$suite"
  mkdir "$output"
  case "$suite" in
    capacity-planner-v1) expected_count=8; required=(capacity_plan.py test_capacity_plan.py) ;;
    capacity-ledger-v1) expected_count=11; required=(capacity_plan.py capacity_ledger.py test_capacity_ledger.py) ;;
    deadline-admission-v1) expected_count=15; required=(deadline_admission.py owned_runtime.py worker_entry.py test_deadline_admission.py test_owned_runtime.py) ;;
  esac
  for name in "${required[@]}"; do
    test -f "$repo/development/$suite/$name"
    sha256sum "$repo/development/$suite/$name" >> "$output/source.properties"
  done
  unit="alpha-ci-${suite}-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}.service"
  sudo systemd-run --unit="$unit" --wait --pipe \
    --property=Type=exec --property=ExitType=main --property=RemainAfterExit=no \
    --property=KillMode=control-group --property=SendSIGKILL=yes \
    --property=TimeoutStopSec=2s --property=RuntimeMaxSec=90s \
    --property=MemoryMax=512M --property=CPUQuota=50% --property=TasksMax=32 \
    --property="User=$(id -u)" --property="Group=$(id -g)" \
    --property=PrivateNetwork=yes --property=NoNewPrivileges=yes \
    --property=ProtectSystem=strict --property=PrivateTmp=yes \
    --property="BindReadOnlyPaths=$repo/development/$suite:/work" \
    --property="BindPaths=$output:/output" --property=WorkingDirectory=/output \
    --setenv=PYTHONPATH=/work --setenv=PYTHONDONTWRITEBYTECODE=1 \
    /bin/bash -euc '
      /usr/bin/systemctl show "$1" -p Type -p ExitType -p RemainAfterExit -p KillMode -p SendSIGKILL -p TimeoutStopUSec -p RuntimeMaxUSec -p MemoryMax -p CPUQuotaPerSecUSec -p TasksMax -p PrivateNetwork -p ProtectSystem > /output/effective.properties
      /usr/bin/python3 -c "import sys,unittest; suite=unittest.defaultTestLoader.discover(\"/work\",pattern=\"test_*.py\"); assert suite.countTestCases()==int(sys.argv[1]), suite.countTestCases(); result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(0 if result.wasSuccessful() else 1)" "$2"
      printf "PASS\n" > /output/tests.result
    ' ci-worker "$unit" "$expected_count" 2>&1 | tee "$output/unit.log"
  [[ $(cat "$output/tests.result") == PASS ]]
  for expected in Type=exec ExitType=main RemainAfterExit=no KillMode=control-group SendSIGKILL=yes 'TimeoutStopUSec=2s' 'RuntimeMaxUSec=1min 30s' MemoryMax=536870912 CPUQuotaPerSecUSec=500ms TasksMax=32 PrivateNetwork=yes ProtectSystem=strict; do
    grep -Fxq "$expected" "$output/effective.properties"
  done
  # The wait has completed; validate the entire owned cgroup, not just MainPID.
  [[ ! -e "/sys/fs/cgroup/system.slice/$unit" ]]
  if [[ "$suite" == deadline-admission-v1 ]]; then
    /usr/bin/python3 - "$output/escaped-descendant.json" "$unit" <<'PY'
import json, pathlib, sys
marker = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert marker['cgroup'].strip() == '0::/system.slice/' + sys.argv[2], marker
assert not pathlib.Path('/sys/fs/cgroup/system.slice', sys.argv[2]).exists()
print('Escaped-session fixture belonged to the owned cgroup; entire cgroup absent after unit completion.')
PY
  fi
  printf 'PASS: whole owned cgroup absent after successful unit completion\n' > "$output/cleanup.result"
  cleanup
  unit=''
done
