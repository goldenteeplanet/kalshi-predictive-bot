# Phase 4OK — Disaster-Recovery Provenance Mutation Resistance

## Outcome

Phase 4OK adds an independent verifier for the multi-anchor disaster-recovery envelope and an
18-case deterministic mutation campaign. The verifier does not call the primary certificate or
provenance acceptance functions. It independently checks exact schemas and fields, nested hashes,
quorum identities and ordering, common statements, frozen capability state, safety invariants,
resource limits, and four externally supplied trust anchors.

Mutations cover deleted votes, duplicated authorities, generation and journal drift, anchor and
copy substitution, state and capability changes, safety exposure, hashes, schemas, ordering,
unknown fields, type confusion, oversized inputs, and verdict changes. Recomputed envelopes remain
invalid because their identities no longer match the external anchors.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OJ/4OK regression: `10 passed in 26.77s`.
- Mutation campaign: `PASS`; mutations: `18`; survivors: `0`.
- Independent of primary acceptance logic: `true`.
- Campaign SHA-256: `49b58d62418dc9a34c5d9806ad8eca600f29337e7df858f5e8a81e98b7c82e22`.

## Safety and removal

All mutations are offline, in-memory, and non-persistent. They cannot create paper orders or enable
demo, live, or autopilot execution. Remove the three Phase 4OK files to roll back.

## Next phase

Phase 4OL — Disaster-recovery end-to-end chaos matrix and recovery-time objective proof.
