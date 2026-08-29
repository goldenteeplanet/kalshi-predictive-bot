# Phase 4OB — Aggregate Gate Mutation and Bypass Resistance

## Outcome

Phase 4OB adds an independently anchored release-candidate verifier and a deterministic mutation
campaign. The verifier enforces exact top-level fields, external manifest identity, phase and check
order, nested hashes and verdicts, schemas, refusal coverage, dependency graph, safety state,
rollback guidance, residual risks, and the settlement boundary.

Twenty-six mutations cover deletion, duplication, reordering, substitution, stale replay, type
confusion, Unicode normalization, oversized input, unknown fields, contradictory verdicts, schema,
phase, file, check, refusal, dependency, safety, rollback, risk, blocker, verdict, and hash changes.
The recomputed-envelope attack updates the outer hash after changing inner evidence and is still
rejected by the external trust anchor. Any survivor, incomplete coverage, nondeterminism, resource
violation, verifier coupling, or safety exposure refuses the campaign.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OA/4OB regression: `13 passed in 124.51s`.
- Campaign verdict: `PASS`.
- Mutations executed: `26`; survivors: `0`; target fields: `13`.
- Trusted aggregate manifest: `9b59d0d54d1da13b139bfb7d574d60c788149b92c0367816f397993e43664aca`.
- Campaign SHA-256: `f9720b030f3b43c8b9040977a120e747475d67334ed85582c42bb60c98da9e5c`.

## Safety and removal

The verifier and mutations are in-memory, offline, and non-persistent, with no infrastructure,
runtime, order, paper, demo, live, or autopilot capability. Remove the three phase files to roll
back.

## Next phase

Phase 4OC — Aggregate evidence freshness, expiration, and renewal proof.
