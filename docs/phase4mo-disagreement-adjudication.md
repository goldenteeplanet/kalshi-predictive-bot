# Phase 4MO — Verifier Disagreement Injection and Adjudication Protocol

## Outcome

Phase 4MO injects deterministic faults into either verifier output across verdict, package and
manifest hashes, component count, every extraction-plan field, exceptions, missing results, and
nondeterministic replay. Every disagreement must localize and keep consensus refused.

## Inert adjudication

Packets hash-bind both raw outputs, both known-good baseline hashes, normalized comparison,
implementation identities, mutation identity, and human-review evidence. Recommendations are
limited to `KEEP_REFUSED`, `FIX_PRIMARY`, `FIX_SECONDARY`, or `FIX_BOTH`. Automatic acceptance is
always false and no recommendation can override consensus.

## Reproducible evidence

- Injected disagreements: 23; refused and localized: 23 of 23.
- Mutation-corpus SHA-256:
  `e9c4dfba5d9f661e3d4e3488e786a1341ac4c7380f3ca8dc78bbab0afff52f98`
- Adjudication-packet set SHA-256:
  `f41830147e1b09fad48f570e0cc6282d4170e27c899f864b58933a268ea5e0f0`
- Complete disagreement audit SHA-256:
  `1b18d7747d0fe0c49876a5c16941695a0da8098486501439007eebe148aed057`

## Safety and removal

Injection and adjudication are bounded, offline, read-only, and in memory. They do not write or
extract packages, access the network, change runtime state, control WSL or services, or create any
order. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4MP — Human-review adjudication workflow simulation and closure evidence.
