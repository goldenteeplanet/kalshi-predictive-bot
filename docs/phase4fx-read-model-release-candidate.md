# Phase 4FX — Read-Model Release Candidate

## Outcome

Phase 4FX composes the validated Phase 4FV provenance dashboard and Phase 4FW differential replay
into a deterministic, hash-protected release-candidate decision. It grants no execution authority.

## Decision contract

- Schema: `phase4fx-read-model-release-candidate-v1`.
- `ACCEPT` requires a healthy dashboard and an exact replay match.
- Warning, stale, stalled, lineage-failure, or replay-divergence evidence produces `REJECT` with
  stable, sorted reasons.
- Missing, malformed, unavailable, partial, or tampered upstream evidence raises a stable
  fail-closed error rather than producing a candidate.
- Source identity, watermark, dashboard hash, replay evidence hash, decision, reasons, and the
  permanent `execution_authorized=false` boundary are covered by canonical SHA-256.

## Safety and verification

The builder consumes in-memory validated artifacts only. It has no database, filesystem, network,
service-control, exchange, or publication surface. Focused tests cover acceptance, each nonhealthy
state, divergence, unavailable and malformed inputs, upstream and result tampering, the safety
boundary, and absence of mutation methods. Ruff and mypy cover phase-owned Python files.

## Rejected alternatives and removal

Advisory acceptance of warning evidence and automatic refresh were rejected because either would
hide uncertainty or broaden authority. Remove the module, focused test, and report to roll back;
no runtime action is needed. Phase 4FY should perform an independent artifact-only review of this
candidate and its complete upstream evidence.
