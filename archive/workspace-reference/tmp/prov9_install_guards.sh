#!/usr/bin/env bash
set -euo pipefail
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/root/prov9_unit_backup_${stamp}
mkdir -p "$backup"
cp -a /etc/systemd/system/kalshi-r5-watcher.service "$backup/"
cp -a /etc/systemd/system/kalshi-multicategory-refresh-scheduler.service "$backup/"
if [[ -d /etc/systemd/system/kalshi-r5-watcher.service.d ]]; then
  cp -a /etc/systemd/system/kalshi-r5-watcher.service.d "$backup/"
fi
if [[ -d /etc/systemd/system/kalshi-multicategory-refresh-scheduler.service.d ]]; then
  cp -a /etc/systemd/system/kalshi-multicategory-refresh-scheduler.service.d "$backup/"
fi
mkdir -p /etc/systemd/system/kalshi-r5-watcher.service.d
mkdir -p /etc/systemd/system/kalshi-multicategory-refresh-scheduler.service.d
cat >/etc/systemd/system/kalshi-r5-watcher.service.d/50-prov9-memory-runtime.conf <<'EOF'
[Service]
MemoryAccounting=true
MemoryHigh=2200M
MemoryMax=2600M
RuntimeMaxSec=9h
EOF
cat >/etc/systemd/system/kalshi-multicategory-refresh-scheduler.service.d/50-prov9-memory-runtime.conf <<'EOF'
[Service]
MemoryAccounting=true
MemoryHigh=1700M
MemoryMax=1900M
TimeoutStartSec=15min
EOF
systemctl daemon-reload
systemd-analyze verify kalshi-r5-watcher.service kalshi-multicategory-refresh-scheduler.service
echo PROV9_UNIT_BACKUP=$backup
systemctl show kalshi-r5-watcher.service kalshi-multicategory-refresh-scheduler.service \
  -p Id -p ActiveState -p SubState -p MainPID -p MemoryCurrent -p MemoryPeak \
  -p MemoryHigh -p MemoryMax -p TimeoutStartUSec -p RuntimeMaxUSec
