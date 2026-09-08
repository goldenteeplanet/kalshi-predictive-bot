# Phase 4GR — Dashboard Staleness Visualization

## Outcome and measured evidence

Phase 4GR converts Phase 4GQ semantics and freshness evidence into deterministic, accessible panel
tokens. Every token includes a text label, age, observed timestamp, severity, and color token, so status
is never communicated by color alone. Focused tests cover deterministic ordering, empty and partial
input, exact aging/stale boundaries, partial and unavailable states, malformed evidence, duplicates,
lineage mismatch, tampering, and immutable safety boundaries.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gr-dashboard-staleness-visualization-v1`.
- Evidence binds panel ID, age, observed timestamp, source identity/watermark, and SHA-256.
- The panel set must exactly match Phase 4GQ and is limited to 32 panels by default.
- At exactly 60 seconds evidence remains `FRESH`; 61 seconds is `AGING`. At exactly 300 seconds it
  remains `AGING`; 301 seconds is `STALE`. Unavailable or failed data is labeled `UNAVAILABLE`.
- Aggregate `FRESH`, `ATTENTION`, and `STALE` states include stable reason codes and a canonical hash.

## Safety, rejected alternatives, rollback, and next dependency

The builder performs no template rendering, query, HTTP request, write, or service control and always
emits `read_only=true` and `execution_authorized=false`. Color-only badges and client-clock-derived age
were rejected because they are inaccessible or nondeterministic.

Rollback is deletion of this module, focused test, and report. Phase 4GS should define deterministic
empty-state explanations that compose with these freshness tokens.
