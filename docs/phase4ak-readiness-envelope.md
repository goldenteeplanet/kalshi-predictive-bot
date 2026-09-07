# Phase 4AK non-executable readiness envelope

Phase 4AK is a strictly read-only boundary between an independently reviewed proposal and any
future executor design. It validates the complete Phase 4AC–4AJ lineage, reconstructs the history
chain, rechecks the human approval, and re-reads settlement state using SQLite URI `mode=ro` with
`PRAGMA query_only=ON` enforced. It neither implements nor authorizes an executor.

The readiness-state precedence is `LINEAGE_FAILURE`, `INPUT_TAMPERED`, `REVIEW_NOT_APPROVED`,
`APPROVAL_EXPIRED_OR_INVALID`, `PROPOSAL_EXPIRED`, `CURRENT_STATE_DRIFTED`,
`EVIDENCE_CONFLICT`, `PRECONDITION_INCOMPLETE`, `READY_FOR_SEPARATE_EXECUTOR_DESIGN`, then
`NO_ELIGIBLE_ROWS`. Cryptographic or structural corruption fails before publication; intact but
inadmissible inputs receive the corresponding readiness state.

Each admitted row preserves semantic compare-and-swap expectations: stable ticker identity,
expected result, expected null `settled_at`, settlement-lineage hash, `updated_at`, evidence
identity and hash, proposed canonical timestamp, and expiration boundaries. These are data only.
Every row declares `executable=false`, `sql_present=false`, and `execution_authorized=false`.

The paired `phase4ak.settlement-mutation-readiness-envelope.v1` and
`phase4ak.executor-design-handoff.v1` artifacts use canonical JSON and deterministic hashes.
Publication writes and `fsync`s temporary files, atomically replaces the final pair, refuses
existing outputs without `--replace`, and restores prior outputs if either replacement fails.

```bash
PYTHONPATH=src python scripts/local/phase4ak_readiness_envelope.py \
  --phase4aj-attestation /tmp/phase4aj-attestation.json \
  --phase4aj-advancement-manifest /tmp/phase4aj-manifest.json \
  --phase4ai-proposal /tmp/phase4ai-proposal.json \
  --phase4ai-review-manifest /tmp/phase4ai-review.json \
  --human-approval-artifact /tmp/human-approval.json \
  --phase4ah-gate-artifact /tmp/phase4ah-gate.json \
  --phase4af-reaudit-artifact /tmp/phase4af-reaudit.json \
  --baseline-phase4af-artifact /tmp/phase4af.json \
  --phase4ag-status-artifact /tmp/phase4ag-status.json \
  --phase4ag-evidence-artifact /tmp/phase4ag-evidence.json \
  --phase4ad-artifact /tmp/phase4ad.json \
  --phase4ae-artifact /tmp/phase4ae.json \
  --history-dir /tmp/phase4ac-history \
  --production-db /path/to/production.db \
  --evaluation-time 2026-08-26T00:00:00Z \
  --readiness-envelope-output /tmp/phase4ak-envelope.json \
  --executor-design-handoff-output /tmp/phase4ak-handoff.json
```

`READY_FOR_SEPARATE_EXECUTOR_DESIGN` means only that the immutable readiness description is
complete. A future executor, fresh revalidation, recoverable snapshot, single-writer lock,
transactional compare-and-swap, one-attempt operator authorization, exact row-count check,
post-mutation audit, and rollback behavior remain separately required and unimplemented.
