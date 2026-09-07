# Phase 4EB — Shared Risk Calculation Audit

Phase 4EB classifies calculations that may be reused as immutable inputs by both Phase 3M
position sizing and Phase 3N advanced risk. Safe reuse requires both consumers, exact
Decimal semantics, decision-independent logic, immutable state, hash-addressed input and
output contracts, and no coupling to either consumer's output.

Any failed property keeps the calculation independent with deterministic reason codes.
Decision outputs are never shared, and reuse does not merge, reorder, or create either
risk decision. Malformed evidence, duplicate identities, unknown consumers, schema drift,
or hash tampering fail closed. The audit is artifact-only and publishes atomically.
