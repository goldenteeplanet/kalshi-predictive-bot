#!/usr/bin/env bash
set -euo pipefail
cd /opt/kalshi-predictive-bot
set -a
. /etc/kalshi-bot/kalshi-bot.env
set +a
echo CURRENT
.venv/bin/alembic current
echo HEADS
.venv/bin/alembic heads
echo TABLE
sqlite3 /var/lib/kalshi-bot/kalshi_phase1.db \
  "SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_provenance_events';"
echo FILE
find alembic/versions -maxdepth 1 -type f -name '*0012*' -print -exec sha256sum {} \;
echo FLAG
grep -E '^RUNTIME_PROVENANCE_DUAL_WRITE_ENABLED=' /etc/kalshi-bot/kalshi-bot.env || true
