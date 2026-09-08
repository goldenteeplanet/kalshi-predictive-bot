# Phase 4HR — Operator Acknowledgement Contract

## Outcome and measured evidence

Phase 4HR validates a hash-protected operator acknowledgement against a validated Phase 4HQ alert
admission. It distinguishes accepted, declined, diagnostics-requested, stale, incomplete, tampered, and
denied outcomes. Records bind an incident, alert decision, issue/acknowledgement/expiry times, action,
and hashed operator identity. No acknowledgement action grants recovery, service control, or restart.

Focused tests cover deterministic acknowledgement, decline and diagnostics outcomes, exact expiry/TTL,
staleness, incomplete/binding/before-issue/future records, malformed actions and bounds, upstream/record/
result tampering, and forbidden delivery/control/mutation surfaces.

## Contract, provenance, freshness, and bounds

- Schema: `phase4hr-operator-acknowledgement-contract-v1`.
- Allowed actions: `ACKNOWLEDGED`, `DECLINED`, and `REQUESTED_DIAGNOSTICS` only.
- Default maximum lifetime: 3,600 seconds; evaluation at exact expiry passes.
- The admission must validate and be `ALLOW`; incident and admission hashes must match exactly.
- Acknowledgement cannot precede issuance or occur after the evaluation time.
- The result redacts acknowledgement identity and binds all timing, identity, action, and denial fields.

## Safety analysis, rejected alternatives, rollback, and next dependency

The contract consumes supplied artifacts only. It cannot deliver alerts, write files, access a database,
control WSL/systemd/the scheduler, or restart Windows. Treating acknowledgement as authorization was
rejected: confirming receipt is not consent to recovery or machine control.

Rollback is deletion of the implementation, focused test, and report. Phase 4HS should define an
authenticated, hash-bound recovery-cancellation command that can only remove authority, never add it.
