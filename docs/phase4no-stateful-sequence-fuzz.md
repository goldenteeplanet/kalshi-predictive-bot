# Phase 4NO — Stateful Sequence Fuzzing

## Outcome

Phase 4NO models canonicalization, bundle creation, mutation, verification, replay, minimization,
archival handoff, interruption, restart, and refusal as explicit transitions. A seeded bounded
generator produces legal flows, repeated commands, invalid ordering, partial bundles, stale
references, duplicate mutations, unsafe mutations, checkpoint recovery, and random adversarial
interleavings.

Every sequence is replayed twice. Event hashes form a continuity chain and retain seed, ordinal, and
sequence provenance. Illegal transitions and unsafe, stale, or partial states are successful only
when they fail closed. Causal refusal sequences can be delta-minimized.

## Verification evidence

- Focused Phase 4NN–4NO suite: `14 passed`
- Seeded sequences replayed twice: `100`
- Terminal states: `92 REFUSED`, `6 ARCHIVED`, `1 VERIFIED`, `1 CANONICAL`
- Refusal classes observed: `5`
- Generation SHA-256: `4d90491f2af0cfcd7baf5a79a1c3968d51342509d840f4c5bad27379a36b2269`
- Stateful-fuzz verdict: `PASS`
- Fuzz SHA-256: `2476071674619f8c3c6eab3e59791105c7bbe7e67edc33f70c5d88521d8c1aa6`

## Safety and removal

The model is entirely in memory, offline, and non-persistent, with no paper, demo, live, autopilot,
order, network, or runtime mutation capability. Remove the three phase files to roll back.

## Next phase

Phase 4NP — Coverage-guided state transition exploration and saturation proof.
