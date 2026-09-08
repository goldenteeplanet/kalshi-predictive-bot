# Phase 4KZ — Commit Payload and Staged-Index Proof

## Outcome

Phase 4KZ adds a deterministic, read-only proof that the Git staged payload contains exactly the
declared phase-owned files and that each staged blob still matches its ordinary worktree file.

## Fail-closed coverage

The verifier refuses undeclared staged paths, missing owned paths, unsafe or duplicate path input,
ambiguous index entries, nonzero merge stages, symlink and submodule modes, missing worktree files,
and index/worktree divergence (including practical intent-to-add cases). The hash-bound result lists
the exact staged paths, modes, and Git object identities.

## Safety and removal

Only read-only Git commands and ordinary-file hashing are used. There is no database, network,
service, writer-lock, artifact publication, or trading capability. Remove the script, focused test,
and this report to roll back the phase.

## Next phase

Phase 4LA — Commit ancestry and parent-state binding.
