# Phase 4NN — Metamorphic Replay Invariants

## Outcome

Phase 4NN declares and verifies transformation contracts for deep copies, key ordering, equivalent
UTC offsets, Unicode and decimal representations, stable scenario enumeration, deterministic
partition/reassembly, harmless metadata removal, and meaningful outcome changes. Each contract says
whether canonical bytes, bundle identity, and replay output must stay the same or change.

The suite also verifies metrics, refusal paths, verdicts, safety state, provenance, normalization
idempotence, transformation-order independence, all 17 adversarial scenarios, and representative
two- and three-factor joint scenarios. Undeclared transformations and incomplete joint fixtures
refuse explicitly.

## Verification evidence

- Focused Phase 4NI and 4NN suite: `15 passed`
- Declared transformations: `9`
- Adversarial scenarios per transformation: `17`
- Representative joint scenarios: `3`
- Metamorphic verdict: `PASS`
- Suite SHA-256: `e97f8e7225f9d5c4cd4b6b8368f99c23a860827e2ceb50f495ab150140d5f105`

## Safety and removal

All transformations and replay checks are offline and non-persistent, with no paper, demo, live,
autopilot, order, network, or runtime mutation capability. Remove the three phase files to roll back.

## Next phase

Phase 4NO — Stateful sequence fuzzing and replay state-machine proof.
