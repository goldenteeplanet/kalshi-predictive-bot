# Phase 4BJ — Two-person review protocol

Phase 4BJ validates, but never creates, an externally supplied two-person approval. It binds the
exact target artifact and exact unique row hashes. Every row requires two non-empty distinct reviewer
identities, separate timezone-aware timestamps, and two independent `APPROVE` decisions.

The effective deadline is the earlier target/approval expiration; equality is expired. Reviews in
the future or at/after the ceiling fail, as do self/duplicate review, missing/duplicate rows, target
drift, invalid provenance, or explicit revocation. Reviewer identities are emitted only as hashes.

Outputs are `phase4bj.two-person-review-validation.v1` and
`phase4bj.two-person-review-refusal.v1`. Focused tests cover distinct identities/timestamps, exact
row and target binding, independent decisions, every duplicate/missing form, provenance, revocation,
future review, exact expiration boundaries, tampering, timezone handling, and the validation-only
static surface.

