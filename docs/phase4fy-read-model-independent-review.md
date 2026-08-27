# Phase 4FY — Read-Model Independent Review

## Outcome and evidence

Phase 4FY adds a deterministic, artifact-only second review of the Phase 4FX release candidate.
The reviewer validates and independently recomputes the candidate from its complete Phase 4FV
dashboard and Phase 4FW replay evidence before considering approval. Focused tests exercise valid,
empty, exact-boundary, stale, malformed, tampered, cross-linked, partial-failure, and mutation-surface
cases. Ruff and the focused pytest suite provide reproducible evidence from committed files.

## Contract and bounded work

- Schema: `phase4fy-read-model-independent-review-v1`.
- Inputs are one candidate, one dashboard, and one replay result; there is no unbounded collection.
- Default inclusive freshness limits are 300 seconds for the snapshot and 900 seconds for progress.
  Evidence exactly at a limit is eligible; evidence one second over is rejected.
- Approval requires a valid 4FX `ACCEPT`, exact independent recomputation, matching lineage hashes,
  and evidence within both freshness limits.
- The output binds all upstream hashes, source identity and watermark, measured ages, configured
  limits, decision, reasons, and `execution_authorized=false` under canonical SHA-256.

## Safety, rejected alternatives, and removal

The reviewer consumes in-memory artifacts and exposes no database, filesystem, network, service,
exchange, or publication method. Missing, partial, malformed, unavailable, or tampered evidence
fails closed. Trusting the candidate verdict without recomputation and auto-refreshing stale evidence
were rejected because they would weaken independence or broaden runtime authority. No UI integration
is added because this is a narrow certification primitive and expensive work should remain opt-in.

Rollback is removal of the module, focused test, and this report; no runtime or data rollback is
needed. Phase 4FZ should certify the complete read-model workstream using the 4FX candidate and this
independent review while preserving the same read-only boundary.
