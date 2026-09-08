# Phase 4OA — Aggregate Adversarial Validation Release Gate

## Outcome

Phase 4OA inventories the actual script, test, and documentation bytes for every phase from 4MZ
through 4NZ, including schema identity and rollback instructions. The aggregate gate requires
passing deterministic evidence for golden, differential, metamorphic, stateful, long-horizon,
checkpoint, quorum, Byzantine, placement, migration, authorization, ceremony, and independent
certification checks. Every check must exercise refusal behavior and preserve the shared offline
safety contract.

The release candidate binds 27 phase records, 13 representative checks, refusal coverage, residual
risks, a 26-edge dependency graph, rollback guidance, and the exact settlement-dependent blocker.
Missing/stale evidence, schema or hash drift, failed or contradictory verdicts, nondeterminism,
untested refusals, execution exposure, dependency gaps, and blocker drift refuse certification.

## Verification evidence

- Full Phase 4MZ and 4N regression: `228 passed in 771.79s`
- Inventoried phases: `27`
- Representative proof categories and refusal classes: `13`
- Dependency edges: `26`
- Inventory SHA-256: `7042249bea828e5bc2040a195ec271c6dd58185374755104153cb511359c2ecb`
- Aggregate-manifest SHA-256: `9b59d0d54d1da13b139bfb7d574d60c788149b92c0367816f397993e43664aca`
- Aggregate verdict: `PASS`
- Certification SHA-256: `230dbd4cdb496bc3d7bef46c5fd97769451ec0cb17a291355ad77571ba5c0015`

## Settlement boundary

The active `KXRAINAUSM-26AUG-1` paper position cannot receive final settlement reconciliation,
post-settlement scoring, or settlement-dependent promotion evidence until the authoritative
September 1, 2026 settlement is available. This does not block the offline adversarial release
candidate and does not authorize any additional paper or live execution.

## Safety and removal

The gate is read-only, offline, and non-persistent. It cannot alter infrastructure or runtime
services, create orders, or enable paper, demo, live, or autopilot execution. Remove the three Phase
4OA files to roll back.

## Next phase

Phase 4OB — Aggregate gate mutation testing and certification bypass resistance.
