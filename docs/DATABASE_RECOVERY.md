# Database Recovery

SQLite remains the default backend at `data/kalshi_phase1.db`. It is fine for
small local runs, but avoid running SQLite from OneDrive during overnight
learning because synced files can lock or corrupt under concurrent writes.

## Health Checks

```bash
kalshi-bot db-health
kalshi-bot db-doctor
kalshi-bot tonight-check
```

`db-health` checks reachability, SQLite PRAGMAs, integrity, OneDrive warnings,
and Alembic status. `db-doctor` also catches missing SQLite files before opening
them.

## Backup

```bash
kalshi-bot sqlite-backup
```

Backups default to `data/backups/*.db`.

## Locked Database

1. Stop `kalshi-bot ui`, `tonight-run`, `learning-run`, and any DB viewer.
2. Wait a few seconds for SQLite locks to clear.
3. Run `kalshi-bot db-doctor`.
4. Restart only one write-heavy loop.

The UI shows: `Database is busy. Try refreshing in a few seconds.`

## Malformed Database

```bash
kalshi-bot sqlite-backup
kalshi-bot sqlite-recover
```

If integrity checks fail, restore a known-good backup or migrate to PostgreSQL
from the latest valid backup. Do not continue overnight learning on a malformed
SQLite file.

## OneDrive Warning

Move the project or at least the SQLite DB to a local, non-synced path before
long runs. PostgreSQL is the preferred backend for overnight data capture.
