# Phase 4OT — Evidence Aging and Settlement-Handoff Readiness

## Outcome

Phase 4OT inventories the aggregate certificate and six critical dependent proofs with explicit UTC
observation, expiration, dependency, and renewal-mode metadata. Expired offline evidence receives a
deterministic dependency-aware renewal schedule. Authoritative settlement evidence is explicitly
settlement-only and cannot be substituted with an offline refresh.

The handoff refuses stale, missing, reordered, altered, unsafe, or circular evidence. Before an
authoritative settlement proof is fresh, it remains settlement-blocked. Even a complete passing
handoff is non-executable: it conveys evidence readiness but grants no trading capability. The
existing paper position and prohibition on additional orders remain explicit.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OS/4OT regression: `11 passed in 50.53s`.
- Pre-settlement handoff: `REFUSE`; settlement ready: `false`; executable: `false`.
- Six expired offline proofs scheduled for renewal; settlement evidence was not synthesized.
- Refusal reason: `SETTLEMENT_HANDOFF_NOT_READY`.
- Handoff SHA-256: `3af17c4f8e379ae12e3bc35fad5109aa1010fb748cad14692e36c1895758fd10`.

## Safety and removal

All aging and scheduling work is offline, in-memory, and non-persistent. It cannot create paper
orders or enable demo, live, or autopilot execution. Remove the three Phase 4OT files to roll back.

## Next phase

Phase 4OU — Offline evidence renewal orchestration and dependency-safe refresh proof.
