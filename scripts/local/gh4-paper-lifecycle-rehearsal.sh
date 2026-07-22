#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$REPO_ROOT"

[[ -x .venv/bin/pytest ]] || {
  echo "Repository virtualenv is missing .venv/bin/pytest." >&2
  exit 1
}

echo "GH-4 local simulated lifecycle rehearsal"
echo "No cloud connection or exchange order path is used."
.venv/bin/pytest -q \
  tests/test_phase_gh4.py \
  tests/test_paper_strategy.py \
  tests/test_paper_ledger_pnl.py
