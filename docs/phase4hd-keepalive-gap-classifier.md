# Phase 4HD — Keepalive Gap Classifier

## Outcome and measured evidence

Phase 4HD classifies bounded, hash-protected keepalive samples as `HEALTHY`, `GAP_DETECTED`,
`STALE`, or `INCOMPLETE`. It reports elapsed-time gaps, missing sequence samples, absent keepalives,
partial observations, freshness, lineage, and alert requirements. Any non-healthy result requires an
alert but does not authorize recovery, service control, host restart, or execution.

Focused tests cover deterministic ordering, exact gap and freshness boundaries, an exceeded gap,
empty and bounded inputs, single/partial/absent/missing-sequence evidence, malformed time and lineage,
tampering, and forbidden query/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hd-keepalive-gap-classifier-v1`.
- At most 128 samples are classified in memory and at least one consecutive pair is required for a
  healthy or gap-detected verdict.
- A gap exactly 60 seconds and evidence exactly 120 seconds old pass; greater values fail closed.
- Sequence gaps are reported as incomplete evidence rather than treated as elapsed-time proof.
- Each sample and the classification bind canonical content with SHA-256.
- Staleness takes precedence over other diagnoses so outdated observations cannot authorize action.

## Safety analysis, rejected alternatives, rollback, and next dependency

The classifier consumes supplied samples only. It cannot query WSL, send keepalives or alerts, control
systemd or the scheduler, access a database, or restart Windows. Treating one delayed sample as restart
authorization was rejected because scheduling delay alone does not prove a host-level failure.

Rollback is deletion of the implementation, focused test, and report. Phase 4HE should produce bounded,
read-only user-systemd reachability evidence while preserving the evidence/control separation.
