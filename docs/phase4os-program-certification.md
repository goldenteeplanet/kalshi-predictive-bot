# Phase 4OS — Pre-Settlement Adversarial Program Certification

## Outcome

Phase 4OS inventories and hashes the script, test, and documentation triplet for every phase from
4OA through 4OR. It verifies schemas, rollback instructions, explicit execution-safety declarations,
phase order, dependency edges, representative regression categories, residual-risk disclosure, and
the unchanged September 1 settlement blocker.

The aggregate certificate binds the complete inventory and regression proof. A separate verifier
recomputes the envelope and inventory hashes and requires externally supplied inventory and
regression anchors. Missing, stale, reordered, contradictory, unsafe, or altered evidence fails
closed. The active paper position is recorded as preserved, with no additional order authorized.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OS tests: `5 passed in 32.80s`.
- Full Phase 4OA–4OS regression: `116 passed in 623.44s`.
- Inventoried phases: `18`; representative categories: `18`.
- Regression proof SHA-256:
  `84a3ed38fa7e767e56c0a3bd90c5bcbfe6310561744eb6a04d01dcc75a80016a`.
- Inventory SHA-256: `542c56964f32587bd9d345245b7cc8c09948119c2518429dd873053caf458662`.
- Aggregate certificate: `PASS`; SHA-256:
  `5eedae70d9794894731a92c630aaec9cae982f1501ad09efa31f16f7be30e7f5`.
- Independent verification: `PASS`; SHA-256:
  `b34025c4d27d6331a7038b9b08957d87f8ddbda656292d6bd41944b37014c152`.

## Safety and removal

Certification is offline, read-only, and non-persistent. It cannot create paper orders or enable
demo, live, or autopilot execution. Remove the three Phase 4OS files to roll back.

## Next phase

Phase 4OT — Post-program evidence aging and September 1 settlement handoff readiness.
