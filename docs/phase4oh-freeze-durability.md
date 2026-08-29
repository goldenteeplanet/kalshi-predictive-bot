# Phase 4OH — Emergency-Freeze Durability and Restart Replay

## Outcome

Phase 4OH adds a deterministic hash-chained journal and checkpoint model for the emergency freeze.
Restart reconstruction verifies every sequence number, prior hash, event hash, evidence verdict,
checkpoint prefix, settlement blocker, and safety invariant before deriving state. Missing or
ambiguous evidence defaults to frozen with capabilities disabled.

A stale valid checkpoint cannot override a newer journal freeze because restoration derives the
current state from the complete verified journal. Missing, truncated, reordered, duplicated,
corrupted, forged, or future checkpoint records fail closed. Only a hash-bound recovery event with
a passing recovery verdict can restore capabilities in this abstract model.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OG/4OH regression: `12 passed in 35.62s`.
- Recovered checkpoint SHA-256:
  `77b751535da75bd8ad2f9fd57a57b6d641bee9a0e0dff1825b05e766ff6a43f0`.
- Newer freeze journal head: `a33c9ec4df52a1528ecdcbede825031bb71d952c652ce68ca46a58fbf9c652d9`.
- Restart state with stale checkpoint: `FROZEN`; capabilities allowed: `false`.
- Restart restoration SHA-256:
  `112315a8d14583f852912768a019f4b598cb0ae24f4a547472d6739ef48b6b25`.
- Missing-state replay: `REFUSE`, `FROZEN`; SHA-256:
  `8393b0133460531e1fd0bb0d643e9238a53a3e01436fd274ae94c2a7a74e4f21`.

## Safety and removal

The durability proof is offline and in-memory; it does not write runtime state or infrastructure.
It cannot create paper orders or enable demo, live, or autopilot execution. Remove the three Phase
4OH files to roll back.

## Next phase

Phase 4OI — Durable-state dual-copy repair and anti-rollback witness anchoring.
