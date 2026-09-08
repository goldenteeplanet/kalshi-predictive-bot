# Phase 4LS — Offline Verifier Mutation Corpus and Compatibility Matrix

## Outcome

Phase 4LS deterministically audits the Phase 4LR verifier with a complete Phase 4LL–4LQ chain. It
emits machine-readable mutation coverage, normalized failure signatures, and schema compatibility
rows while proving canonical input-order invariance.

## Coverage

The corpus covers wrapper deletion, addition and type mutation; hash substitution across every
schema; dependency rewiring; duplicate identity; cycles; truncation; oversized content; sensitive
value injection; and schema type mutation across every allowlisted artifact. Current v1 schemas pass;
explicit v0 and v2 variants fail closed.

## Reproducible report

The synthetic complete-chain audit passes with 22 of 22 unsafe mutations refused, six of six
current schemas covered, 18 compatibility cases, and canonical order invariance. Audit SHA-256:
`0b74a90bfbe3a75c74348c22efc1e4f89f8de30430c22b1fa23afceec4f4deed`.

The corpus exposed and corrected a Phase 4LR edge case where a non-string schema could raise an
exception. It now returns the stable `SCHEMA_TYPE_INVALID` refusal instead.

## Safety and removal

The audit operates on in-memory copies and cannot mutate files, access a network or database,
control services or WSL, deliver notifications, or create orders. Remove the script, focused test,
and report to roll back.

## Next phase

Phase 4LT — Cross-platform canonicalization and locale/timezone invariance audit.
