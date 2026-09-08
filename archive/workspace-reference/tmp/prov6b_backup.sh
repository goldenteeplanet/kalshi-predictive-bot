#!/usr/bin/env bash
set -euo pipefail

DB=/var/lib/kalshi-bot/kalshi_phase1.db
DEST=/mnt/kalshi-backup
STATUS="$DEST/prov6b_backup_status.env"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TMP="$DEST/kalshi_phase1_pre_prov6_${STAMP}.db.tmp"
FINAL="$DEST/kalshi_phase1_pre_prov6_${STAMP}.db"

exec > >(tee -a "$DEST/prov6b_backup.log") 2>&1
trap 'rc=$?; printf "status=failed\nexit_code=%s\nupdated_utc=%s\n" "$rc" "$(date -u +%FT%TZ)" > "$STATUS"; exit "$rc"' ERR

mountpoint -q "$DEST"
[[ $(findmnt -n -o SOURCE --target "$DEST") == /dev/sda ]]
[[ $(blockdev --getsize64 /dev/sda) -eq 53687091200 ]]

for fd in /proc/[0-9]*/fd/*; do
  target=$(readlink "$fd" 2>/dev/null || true)
  case "$target" in
    "$DB"|"$DB-wal"|"$DB-shm") echo "Database holder found at $fd" >&2; exit 20 ;;
  esac
done

printf "status=running\nstarted_utc=%s\ntemp_path=%s\nfinal_path=%s\n" "$(date -u +%FT%TZ)" "$TMP" "$FINAL" > "$STATUS"

flock -n /var/lib/kalshi-bot/prov6b-backup.lock \
  sqlite3 "$DB" ".timeout 60000" ".backup '$TMP'"

integrity=$(sqlite3 "$TMP" "PRAGMA integrity_check;")
[[ "$integrity" == "ok" ]]
mv "$TMP" "$FINAL"
sync "$FINAL"
sha=$(sha256sum "$FINAL" | awk '{print $1}')
bytes=$(stat -c %s "$FINAL")
printf '%s  %s\n' "$sha" "$(basename "$FINAL")" > "$FINAL.sha256"
chown kalshi:kalshi "$FINAL" "$FINAL.sha256"
chmod 0640 "$FINAL" "$FINAL.sha256"

cat > "$STATUS" <<EOF
status=complete
started_utc=$STAMP
completed_utc=$(date -u +%FT%TZ)
backup_path=$FINAL
backup_bytes=$bytes
integrity_check=$integrity
sha256=$sha
EOF
chown kalshi:kalshi "$STATUS" "$DEST/prov6b_backup.log"
chmod 0640 "$STATUS" "$DEST/prov6b_backup.log"
