#!/usr/bin/env bash
set -euo pipefail

readonly WRITER_LOCK="/home/james/kalshi-local-runtime/kalshi-writer.lock"
readonly SOURCE="/mnt/c/Users/user1/OneDrive/Documents/Dejoia Trading Bot/kalshi-predictive-bot/src/kalshi_predictor/phase_gh2.py"
readonly TARGET="/home/james/kalshi-runtime-src/src/kalshi_predictor/phase_gh2.py"

flock "$WRITER_LOCK" cp "$SOURCE" "$TARGET"
grep -q 'ranking_repair_limit=exact_snapshot_refresh_limit' "$TARGET"
date -Is > /home/james/kalshi-local-runtime/install-phase-gh2.complete
