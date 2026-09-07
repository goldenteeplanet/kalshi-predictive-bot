#!/usr/bin/env bash
set -euo pipefail

STATUS=/mnt/kalshi-backup/prov6b_backup_status.env
while systemctl is-active --quiet kalshi-prov6b-backup.service; do
  sleep 30
done

grep -qx 'status=complete' "$STATUS"
systemctl start kalshi-ui.service
systemctl start kalshi-multicategory-refresh-scheduler.timer

{
  echo "restored_utc=$(date -u +%FT%TZ)"
  echo "ui_active=$(systemctl is-active kalshi-ui.service || true)"
  echo "scheduler_timer_active=$(systemctl is-active kalshi-multicategory-refresh-scheduler.timer || true)"
} >> "$STATUS"
