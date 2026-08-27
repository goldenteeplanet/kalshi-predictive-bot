# Phase 4GO — Dashboard Progressive Disclosure

## Outcome and measured evidence

Phase 4GO adds a deterministic disclosure plan over a validated Phase 4GN registry. Cheap panels receive
bounded summaries; expensive panels remain `DEFERRED` until explicitly requested; unavailable or stale
panels remain `UNAVAILABLE`. Focused tests cover stable ordering, empty and partial input, exact detail
and freshness bounds, opt-in detail, unavailable evidence, malformed input, duplicates, mixed lineage,
tampering, and immutable safety boundaries.

## Inputs, outputs, provenance, freshness, and bounds

- Schema: `phase4go-dashboard-progressive-disclosure-v1`.
- Inputs bind panel ID, cost class, availability, explicit detail request, source identity/watermark, and
  evidence age. Every input and the final plan is protected by canonical SHA-256.
- The input panel set must exactly match the registry and preserve its navigation order and lineage.
- At most 32 inputs and four requested expensive detail panels are accepted by default.
- Evidence exactly 300 seconds old remains eligible. Older evidence, a stale registry, a blocked
  registry, malformed or partial input, lineage mismatch, or tampering fails closed.
- Only an available expensive panel with an explicit request receives `DETAIL` and
  `detail_query_allowed=true`; no hidden eager query is authorized.

## Safety, rejected alternatives, rollback, and next dependency

The planner is pure computation. It performs no database or HTTP query, writes no artifact, controls no
service, and always emits `read_only=true` and `execution_authorized=false`. Eager detail loading and
implicit expansion based on viewport position were rejected because they add unpredictable query load.

Rollback is deletion of this module, focused test, and report. Phase 4GP should map these disclosure
modes into explicit loading states without treating deferred or unavailable work as an error.
