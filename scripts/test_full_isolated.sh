#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/kalshi-full-suite.XXXXXX")"
trap 'rm -rf "$test_root"' EXIT

unset DATABASE_URL
export KALSHI_DB_URL="sqlite:///$test_root/kalshi_test.db"
export DB_BACKEND="sqlite"
export EXECUTION_ENABLED="false"
export AUTOPILOT_ENABLED="false"
export LEARNING_ACCELERATION_ENABLED="false"

cd "$repo_root"
if (($#)); then
    exec "${PYTHON:-$repo_root/.venv/bin/python}" -m pytest "$@"
fi

exec "${PYTHON:-$repo_root/.venv/bin/python}" -m pytest -q tests
