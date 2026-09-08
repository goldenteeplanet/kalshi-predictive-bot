# Phase 4KY — Phase-Owned Change Boundary Verifier

## Outcome

Phase 4KY compares two hash-bound Phase 4KX manifests against an explicit phase-owned path list.
It passes only when every unrelated worktree record is unchanged and unstaged and every owned path
is present. This protects a busy worktree from accidental cross-phase commits.

## Refusal coverage

The verifier refuses malformed schemas, manifest hash mismatches, count mismatches, duplicate or
unsafe paths, undeclared new changes, modifications or removal of pre-existing user changes,
unrelated staged files, and missing phase-owned files. Results are deterministic and read-only.

## Safety and removal

The implementation reads JSON inputs only and has no database, service, network, writer-lock, or
trading capability. Remove the script, focused test, and this report to roll back the phase.

## Next phase

Phase 4KZ — Commit payload and staged-index proof.
