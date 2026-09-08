#!/usr/bin/env bash
set -euo pipefail
cd /mnt/kalshi-backup
sha256sum -c kalshi_phase1_pre_prov6_20260716T235750Z.db.sha256
printf 'verified_utc=%s\n' "$(date -u +%FT%TZ)" > prov6c_backup_reverify.env
printf 'sha256_verified=true\n' >> prov6c_backup_reverify.env
chown kalshi:kalshi prov6c_backup_reverify.env
chmod 0640 prov6c_backup_reverify.env
