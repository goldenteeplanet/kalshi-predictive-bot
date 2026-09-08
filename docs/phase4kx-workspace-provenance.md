# Phase 4KX — Workspace Provenance Manifest

## Outcome

Phase 4KX adds a deterministic, read-only inventory of outstanding Git worktree changes. Later
phases can use the manifest to preserve pre-existing user work and restrict commits to explicitly
owned files.

## Safety

- Reads only Git metadata and ordinary worktree files.
- Does not open any trading database or artifact store.
- Does not control WSL, systemd, the scheduler, or UI.
- Has no forecast, ranking, risk-decision, order, fill, settlement, or execution capability.
- Produces JSON only when an explicit output path is supplied.

## Verification

Focused tests prove deterministic ordering and hashing, clean-repository behavior, and byte-for-byte
preservation of Git status. Formatting, lint, the phase-owned diff, runtime services, and fail-closed
settings must pass before commit.

## Removal

Remove the phase script, focused test, and this report. No database or runtime rollback is needed.

## Next phase

Phase 4KY — Phase-owned change boundary verifier.
