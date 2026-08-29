# Phase 4OC — Aggregate Evidence Freshness, Expiration, and Renewal

## Outcome

Phase 4OC binds the Phase 4OA aggregate manifest to a deterministic, hash-addressed freshness
proof. Proofs have an explicit UTC observation time, six-hour maximum validity window, exact
expiration boundary, monotonic sequence, and parent hash. The verifier uses an externally supplied
manifest anchor, rejects future-dated, expired, overlong, malformed, or altered evidence, and keeps
the September 1 settlement dependency explicit.

Renewal is permitted only while the prior proof is still valid. A renewal preserves the aggregate
manifest identity, advances time and sequence, and links to the prior proof hash. The independent
chain verifier checks every proof at the next renewal boundary, preventing gaps, forks, relabeling,
or resurrection of expired evidence. Identical inputs produce identical proof bytes and hashes.

## Verification evidence

- Ruff: passed.
- Combined Phase 4OA–4OC regression: `20 passed in 146.71s`.
- Aggregate manifest: `9b59d0d54d1da13b139bfb7d574d60c788149b92c0367816f397993e43664aca`.
- Initial proof: `1d31d41070bb2821430c24f07e789d05bcc27982a9664540d9d7a6a6eb832f27`.
- Renewal proof: `c5ca872efaf4686c586aab44ec4d3a68a10902697f9731dc9d89ccfbefed3af6`.
- Independent chain verdict: `PASS`; verification SHA-256:
  `7aa929836d2836e24ff41fbac8112f9bc91f3dffd52445a9e5ea7d279e665b99`.

## Safety and removal

All work is offline, in-memory, and non-persistent. It cannot mutate infrastructure or runtime state
and cannot create paper orders or enable demo, live, or autopilot execution. Remove the three Phase
4OC files to roll back.

## Next phase

Phase 4OD — Freshness-renewal race, clock rollback, and split-brain resistance.
