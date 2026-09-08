# Phase 4GW — Dashboard Responsive Layout Audit

## Outcome and measured evidence

Phase 4GW adds a deterministic, artifact-only responsive-layout audit. Supplied viewport samples bind
dimensions, horizontal overflow, minimum touch-target size, required-content visibility, navigation
reachability, completeness, freshness, lineage, and SHA-256 integrity. Focused tests cover valid and
order-independent evaluation, empty input, resource bounds, exact touch-target and freshness edges,
staleness, malformed values, partial failure, duplicates, mixed lineage, and tampering.

## Contract, provenance, freshness, and bounds

- Schema: `phase4gw-dashboard-responsive-layout-audit-v1`.
- Samples carry a source identity and watermark; mixed lineage and duplicate sample IDs fail closed.
- At most 32 samples are evaluated by default, with no browser, network, filesystem, or database work.
- A 44-pixel touch target and evidence age of exactly 300 seconds pass. A smaller target fails and
  evidence one second older becomes `STALE`.
- Any horizontal overflow, hidden required content, unreachable navigation, or incomplete sample
  produces a stable sample-specific violation.

## Safety analysis, rejected alternatives, rollback, and next dependency

The evaluator consumes already-collected evidence only and always emits `read_only=true` and
`execution_authorized=false`. Live browser automation and automatic CSS/template rewriting were
rejected because they add mutation and nondeterminism to a certification boundary. Production DB,
orders, forecasts, artifacts, services, and safety settings are untouched.

Rollback is deletion of this module, focused test, and report. Phase 4GX is the next dependency and
must consume only independently verified, hash-protected dashboard evidence.
