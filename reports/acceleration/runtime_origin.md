# Runtime Origin

Generated: `2026-08-21T21:10:00.205389+00:00`
Status: `READY`
All statuses: `READY`

## Source identity

- CWD: `/home/james/kalshi-runtime-src`
- Development root: `/home/james/kalshi-main-baseline`
- Runtime source: `/home/james/kalshi-runtime-src`
- Installed package: `/home/james/kalshi-runtime-src/src/kalshi_predictor/__init__.py`
- Branch: `fix/shadow-preflight-isolation-20260821`
- Development SHA: `80ea28d648e400ccbd25119de1a07ec17e092a9e`
- Deployed SHA: `80ea28d648e400ccbd25119de1a07ec17e092a9e`
- Python: `/home/james/kalshi-runtime-src/.venv/bin/python`
- Virtualenv: `/home/james/kalshi-runtime-src/.venv`

## Persistent paths

- Database: `/home/james/kalshi-predictive-bot-data/kalshi_phase1.db`
- Report root: `/home/james/kalshi-runtime-src/reports`
- UI report root: `/mnt/d/KalshiPredictiveBotStorage/windows-kalshi-predictive-bot/reports`
- Research data root: `/mnt/d/KalshiPredictiveBotStorage/windows-kalshi-predictive-bot/data/research`
- Writer lock: `/home/james/kalshi-local-runtime/kalshi-writer.lock`
- Staging root: `/home/james/kalshi-runtime-src/reports/staging`

## Layout decision

- Intentional development/runtime separation: `True`
- Installed package under runtime root: `True`
- Database outside both code checkouts: `True`
- Separate UI publishing path: `True`

The separated layout is retained. Duplicate launch wrappers are not consolidated until the
deployed manifest and this command both report `READY`.

## Legacy-reference audit

- `/home/james/projects/kalshi-predictive-bot` exists but is not a Git checkout. It is not an
  authoritative development or runtime source.
- The OneDrive-facing report path is an intentional publishing link into
  `/mnt/d/KalshiPredictiveBotStorage`; it is not used for SQLite, writer locks, runtime imports,
  staging, or research data.
- `scripts/nightly_paper_runner.sh` retains a fallback to the old `~/projects/.../data` layout.
  It is outside the fixed-rate runtime and must be classified obsolete before removal.
- `scripts/local/install-phase-gh2-when-idle.sh` copies a single file from the Windows checkout
  into runtime source. It is a legacy partial-deployment mechanism and must not be used now that
  the deployment manifest identifies the complete authoritative source SHA.
- `/var/lib/kalshi-bot` references belong to cloud-only GH-1/GH-2 launchers and are not local
  path mismatches.

## Runtime-origin status contract

The command can emit `READY`, `PATH_MISMATCH`, `OLD_RUNTIME`, `ONEDRIVE_REFERENCE`,
`EDITABLE_INSTALL_MISMATCH`, or `DATABASE_PATH_MISMATCH`. The deployed check returned only
`READY`; development and deployed SHAs match exactly.
