# Phase 4OI — Dual-Copy Repair and Anti-Rollback Anchoring

## Outcome

Phase 4OI wraps independent durable-state copies in content hashes and validates them against an
external monotonic anchor. A copy must replay cleanly, contain the anchored generation and exact
anchored journal head, and preserve safety metadata before it can be considered.

Equal copies converge deterministically. If one valid history is an unambiguous descendant, it is
canonical and produces an in-memory repair plan for the stale or damaged peer. Divergent valid
histories, copies behind the anchor, forged anchors, duplicate identities, simultaneous corruption,
or ambiguous ancestry fail closed in the frozen state. Repair never writes runtime state.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OH/4OI regression: `13 passed in 31.38s`.
- External anchor SHA-256: `9d2d7cb19413f9a20ab901c2f08eeca7aa4630c48e5e21ec7a5f372611fec31a`.
- Descendant reconciliation: `PASS`, `FROZEN`; SHA-256:
  `85a167a62df4703bc4b9364a2ba70283c574469df1973ecb400f681c1a8f57c6`.
- Canonical head: `a542d626d1a4ab3236af42d108acfa4142939cd939c5caf71917b463392e6c98`;
  repair target: `old`.
- Divergent histories: `REFUSE`, `FROZEN`, with `DIVERGENT_VALID_HISTORIES`;
  SHA-256: `a4f690d69e4a831479a0cbc4b9f1fdd5111bd07a40f2a1d6f93d596f41c7b02a`.

## Safety and removal

The reconciliation and repair plan are offline, in-memory, and non-persistent. They cannot create
paper orders or enable demo, live, or autopilot execution. Remove the three Phase 4OI files to roll
back.

## Next phase

Phase 4OJ — Multi-anchor quorum, anchor equivocation, and disaster-recovery provenance.
