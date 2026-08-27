# Phase 4FV — Read-Model Provenance Dashboard

## Outcome

Phase 4FV adds a deterministic, hash-protected dashboard view model and reusable Jinja panel for
the Phase 4FN–4FU read-model evidence chain. It presents schema compatibility, source identity,
watermark, payload/manifest hashes, chain range, freshness, progress age, escalation action, and an
explicit paper/read-only safety boundary.

## Honest state model

- Any missing upstream component renders `UNAVAILABLE` with an exact missing-component list.
- Incompatible schema renders `BLOCKED`.
- Lineage failure, stalled progress, stale snapshot, and warning retain their distinct states.
- Only compatible, fresh, valid evidence renders `HEALTHY`.
- `execution_authorized` is always false and is validated as part of the dashboard hash contract.

The artifact identity is `phase4fv-read-model-provenance-dashboard-v1`. Canonical SHA-256 covers
the complete model, including unavailable states. Evidence-lane fields are bounded before copying.

## UI integration decision

The phase-owned Jinja macro renders both healthy and unavailable states and can be included by the
existing evidence dashboard. The current `/evidence` route and base navigation contain unrelated
uncommitted user changes, so this phase deliberately does not modify or stage those files. This
preserves worktree ownership and avoids accidentally committing the existing dashboard rollout.

## Safety and verification

The builder consumes only already-validated frozen upstream results. It performs no database,
filesystem, network, exchange, service-control, publication, or mutation operation. Tests cover
healthy provenance, missing evidence, status precedence, identity mismatch, lane bounds,
tampering, partial models, real Jinja rendering, immutability, and absence of writer methods.
The cumulative Phase 4FN–4FV regression passed 87 tests in 70.14 seconds. Ruff and mypy passed
for the phase-owned files.

## Rejected alternatives

- Editing the dirty shared route/template was rejected to preserve unrelated user work.
- Showing partial values as healthy was rejected; missing evidence is explicit.
- Adding dashboard refresh controls was rejected because this surface is observation-only.

## Removal and next dependency

Remove the view-model module, Jinja panel, focused test, and report. No runtime rollback is needed.
Phase 4FW should replay equivalent evidence inputs through independent paths and compare the
resulting provenance dashboard semantics.
