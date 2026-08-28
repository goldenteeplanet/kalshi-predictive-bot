# Phase 4LA — Commit Ancestry and Parent-State Binding

## Outcome

Phase 4LA adds a deterministic post-commit verifier binding a phase commit to one expected parent,
the active branch head, its exact Git tree, its declared committed paths, and a hash-valid passing
Phase 4KZ staged-payload proof.

## Refusal coverage

The verifier refuses missing commits, root or merge commits, unexpected parents or branches,
detached HEAD, non-head commits, tree mismatches, undeclared or missing committed paths, unsafe path
input, malformed or failing staged proofs, proof-path disagreement, and proof tampering.

## Safety and removal

All Git operations are read-only. Tests create disposable repositories only. There is no database,
network, service-control, writer-lock, artifact-publication, or trading capability. Remove the phase
script, test, and report to roll back.

## Next phase

Phase 4LB — Commit evidence receipt and chain ledger.
