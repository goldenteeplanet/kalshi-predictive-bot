# Phase 4OO — Baseline Lineage, Retention, and Rollback

## Outcome

Phase 4OO stores recovery baselines in an immutable hash-linked history. Versions append exactly
once; genesis has no promotion evidence, and every later version binds the comparison that approved
it. Verification enforces sequence, ancestry, unique baseline and entry identities, trusted head,
minimum retention, claim semantics, and safety state.

Rollback is a separate frozen operational proposal referencing an older retained baseline. It does
not rewrite the head, delete history, restore capabilities, or assert a new performance claim.
History deletion or rewriting, version gaps, stale heads, missing promotion evidence, replay, forks,
and invalid rollback targets fail closed.

## Verification evidence

- Ruff: passed.
- Focused Phase 4ON/4OO regression: `13 passed in 30.75s`.
- Two-version lineage head: `0a11680660e082153ab3b9fb80b15e2f19ffc47432157caddedd4134ccd72476`.
- Lineage verification: `PASS`; SHA-256:
  `9b1d213bab7e5224dce47f6fb7d94033114700d7493704d0a9323eff5d6aa47c`.
- Version-1 rollback: `PASS`, `FROZEN_OPERATIONAL_ROLLBACK`, no performance claim, and
  capabilities disabled.
- Rollback SHA-256: `b1ec7c5bfa420a06a6b9ffb03b103c9f14b6bcd7e4ee20de2a3ab0d282ff61a8`.

## Safety and removal

All lineage and rollback artifacts are offline, in-memory, and non-persistent. They cannot create
paper orders or enable demo, live, or autopilot execution. Remove the three Phase 4OO files to roll
back.

## Next phase

Phase 4OP — Baseline-lineage independent audit and retention-loss recovery proof.
