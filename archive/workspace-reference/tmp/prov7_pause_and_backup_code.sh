#!/usr/bin/env bash
set -euo pipefail

bash /root/prov7_pause_readers.sh
cd /opt/kalshi-predictive-bot
dest=/root/prov7_code_backup_20260717
mkdir -p "$dest/src/kalshi_predictor/data" "$dest/src/kalshi_predictor/opportunities"
cp src/kalshi_predictor/config.py "$dest/src/kalshi_predictor/config.py"
cp src/kalshi_predictor/data/repositories.py "$dest/src/kalshi_predictor/data/repositories.py"
cp src/kalshi_predictor/opportunities/repository.py "$dest/src/kalshi_predictor/opportunities/repository.py"
cp src/kalshi_predictor/data/schema.py "$dest/src/kalshi_predictor/data/schema.py"
echo "$dest"
