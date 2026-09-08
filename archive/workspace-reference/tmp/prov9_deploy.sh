#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/root/prov9_code_backup_${stamp}
mkdir -p "$backup"
cp src/kalshi_predictor/paper/ledger.py "$backup/ledger.py"
cp src/kalshi_predictor/opportunities/scanner.py "$backup/scanner.py"
cp src/kalshi_predictor/phase3bb_r8_unified_paper_gate.py "$backup/phase3bb_r8_unified_paper_gate.py"
cp tests/test_phase_2_6_opportunities.py "$backup/test_phase_2_6_opportunities.py"
if [[ -f tests/test_phase3bb_r8_unified_paper_gate.py ]]; then
  cp tests/test_phase3bb_r8_unified_paper_gate.py "$backup/test_phase3bb_r8_unified_paper_gate.py"
fi
install -m 0644 /tmp/prov9/ledger.py src/kalshi_predictor/paper/ledger.py
install -m 0644 /tmp/prov9/scanner.py src/kalshi_predictor/opportunities/scanner.py
install -m 0644 /tmp/prov9/phase3bb_r8_unified_paper_gate.py src/kalshi_predictor/phase3bb_r8_unified_paper_gate.py
install -m 0644 /tmp/prov9/test_phase_2_6_opportunities.py tests/test_phase_2_6_opportunities.py
install -m 0644 /tmp/prov9/test_phase3bb_r8_unified_paper_gate.py tests/test_phase3bb_r8_unified_paper_gate.py
chown kalshi:kalshi src/kalshi_predictor/paper/ledger.py \
  src/kalshi_predictor/opportunities/scanner.py \
  src/kalshi_predictor/phase3bb_r8_unified_paper_gate.py
./.venv/bin/python -m py_compile \
  src/kalshi_predictor/paper/ledger.py \
  src/kalshi_predictor/opportunities/scanner.py \
  src/kalshi_predictor/phase3bb_r8_unified_paper_gate.py
echo PROV9_BACKUP=$backup
