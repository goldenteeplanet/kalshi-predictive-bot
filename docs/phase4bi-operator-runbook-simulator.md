# Phase 4BI — Operator runbook simulator

Phase 4BI models the complete operator workflow as eight exact stages: artifact collection,
independent verification, human review, readiness compilation, authorization validation, disposable
simulation, rollback confirmation, and final refusal/non-production handoff.

Every step binds the previous transcript hash. Reordering, skipping an unblocked stage, continuing
after refusal, simulation without validated authorization, nonterminal refusal, or handoff without
rollback fails closed. A refusal at any intermediate stage requires all remaining intermediate stages
to be skipped and terminates in `FINAL_REFUSAL`; only a fully passing disposable flow can end in
`HANDOFF_NON_PRODUCTION`.

Outputs are `phase4bi.operator-runbook-transcript.v1` and
`phase4bi.workflow-safety-proof.v1`. Focused tests cover the full handoff path, refusal at each of
seven intermediate stages, reorder/bypass/lineage attacks, unsafe continuation, invalid terminal
states, tampering, timezone handling, deterministic transcript hashes, and the non-executing static
surface.

