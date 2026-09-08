# Phase 4HI — Recovery Evidence Canonicalization

## Outcome and measured evidence

Phase 4HI validates and canonicalizes the exact Phase 4HB–4HH evidence chain in fixed phase order.
The bundle binds each upstream status, evidence hash, age, source set, threshold, and aggregate digest.
Only stable/available/healthy/reachable/passed statuses produce `READY`; stale, incomplete, or denied
inputs remain explicit. Readiness is a prerequisite and never authorizes recovery, service control,
host restart, or execution.

Focused tests cover the complete deterministic chain, a denied upstream status, exact freshness and
staleness, incomplete evidence, wrong types, upstream tampering, entry-order/hash/result tampering,
and forbidden query/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hi-recovery-evidence-canonicalization-v1`.
- Exact phase order: 4HB, 4HC, 4HD, 4HE, 4HF, 4HG, 4HH.
- Every artifact must pass its phase-specific validator before inclusion.
- Evidence exactly 120 seconds old passes; any older or explicitly stale artifact makes the bundle stale.
- Entries, phase order, source set, thresholds, verdict, and denied capabilities are SHA-256-bound.
- The canonical bundle contains hashes and status metadata, not raw probe output or database rows.

## Safety analysis, rejected alternatives, rollback, and next dependency

The canonicalizer consumes validated in-memory evidence only. It cannot query WSL, processes, systemd,
the scheduler, or a database; send notifications; or control/restart anything. Accepting an unordered or
partially validated artifact set was rejected because it would weaken lineage and omit prerequisites.

Rollback is deletion of the implementation, focused test, and report. Phase 4HJ should independently
detect tampering in recovery decisions derived from this canonical bundle.
