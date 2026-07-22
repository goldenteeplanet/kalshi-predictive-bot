# Commit-based cloud deployment

The cloud runtime must be deployed from an immutable commit on `main`. Do not copy an
uncommitted worktree over `/opt/kalshi-predictive-bot`.

## Required evidence

Before a write-capable deployment, preserve:

- the intended Git commit SHA and a clean local worktree;
- passing targeted tests and the isolated full-suite result;
- a verified database backup with `quick_check=ok`, `integrity_check=ok`, and SHA-256;
- rollback copies or the previously deployed commit SHA;
- `EXECUTION_ENABLED=false`;
- `db-writer-monitor` showing zero writers and `safe_to_start_write=true`;
- `db-locks` showing no writer;
- competing writer services and timers inactive;
- the legacy 32-cycle watcher disabled.

Stop on any failed gate. Never use deployment to enable paper or live execution.

## Guarded deployment outline

Generate the fail-closed preparation manifest before touching the cloud checkout:

```bash
.venv/bin/python scripts/cloud/prepare-exact-sha-deployment.py \
  --target-sha "$TARGET_SHA" \
  --rollback-sha "$ROLLBACK_SHA" \
  --environment-file /path/to/redacted-runtime-copy.env \
  --backup-database /path/to/verified-backup.db \
  --output /path/to/deployment-preflight.json
```

The command is read-only with respect to Git, configuration, and the database. It refuses
a dirty checkout, a target other than `origin/main`, unsafe execution flags, a missing or
invalid rollback commit, or a backup that fails SQLite quick/full integrity checks. The
manifest records hashes but never environment secret values. Producing it does not
authorize deployment, paper orders, or live execution.

1. Fetch the repository without altering the active checkout.
2. Verify the approved commit exists on `origin/main` and record its SHA.
3. Record the current cloud commit as the rollback target.
4. Re-run execution, writer, lock, service-isolation, capacity, and backup gates.
5. Check out the approved commit in the cloud application directory using the established
   deployment mechanism.
6. Run bounded, read-only smoke tests first.
7. Run a write-capable certification only when separately approved and after fresh gates.
8. Confirm output parity, provenance validity, locks released, and execution still disabled.
9. Roll back to the recorded commit immediately on any failed smoke or safety gate.

Cloud reports, backups, and operational evidence are not repository source files and must
not be deleted or overwritten by a checkout.

## Rollback evidence

Every deployment report must include:

- deployed and rollback commit SHAs;
- changed file list;
- backup path and SHA-256;
- pre/post execution, writer, and lock states;
- smoke-test and certification results;
- rollback command preview and whether rollback was exercised.
