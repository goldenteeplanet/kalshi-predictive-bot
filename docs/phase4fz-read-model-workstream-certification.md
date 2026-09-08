# Phase 4FZ — Read-Model Workstream Certification

## Outcome and measured evidence

Phase 4FZ adds a deterministic final certificate for the Phase 4FM–4FY read-model workstream. It
consumes one validated Phase 4FX release candidate and one Phase 4FY independent review, verifies
their release, identity, and watermark links, and emits a hash-protected certification result. The
focused suite measures valid, empty, exact-boundary, stale, malformed, tampered, partial, rejected,
and mutation-surface paths; Ruff and pytest results are reproducible from the committed files.

## Contract, freshness, and resource bounds

- Schema: `phase4fz-read-model-workstream-certification-v1`.
- Inputs are exactly one candidate and one review; no database scan or unbounded collection exists.
- The default inclusive maximum evidence age is 300 seconds. The effective age is the greater of
  the independently reviewed snapshot and progress ages. Exactly 300 is eligible; 301 is stale.
- `CERTIFIED` requires a 4FX `ACCEPT`, a 4FY `APPROVE`, exact cross-artifact hash lineage, and fresh
  evidence. Any ordinary gate failure produces `NOT_CERTIFIED`; malformed, partial, unlinked, or
  tampered evidence raises a stable fail-closed error.
- Canonical SHA-256 covers the decision, reasons, lineage, ages, configured bound, and permanent
  `execution_authorized=false` boundary.

## Safety, alternatives, rollback, and next dependency

Certification is in-memory and has no database, filesystem, network, publication, service-control,
or exchange surface. Automatic evidence refresh and treating review approval as execution authority
were rejected because both broaden the phase beyond certification. UI integration is intentionally
omitted; a future UI may display a supplied certificate but must not compute it on a default path.

Rollback is deletion of the module, focused test, and report, requiring no runtime or data change.
Phase 4GA should begin the next program workstream using this certificate solely as read-only
evidence and must retain independent safety gates.
