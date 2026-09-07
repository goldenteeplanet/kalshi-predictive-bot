# Phase 4AI deterministic canonicalization proposal

Phase 4AI consumes the hash-validated Phase 4AH gate and its complete Phase 4AC–4AG
lineage, then rechecks the current settlement rows through a SQLite `mode=ro` connection
with `query_only` enforced. It emits a deterministic proposal for independent review; it
does not execute, authorize, or provide an execution interface for any database change.

Each proposed row records the candidate canonical timestamp, the exact source evidence,
the current settlement lineage hash, and explicit compare-and-swap preconditions. A row is
excluded when the Phase 4AH transition is ineligible, evidence is missing or stale, a
canonical timestamp has appeared, settlement lineage has changed, a linked evaluation has
appeared, or any timestamp/result conflict is present. Input schema, row hashes, artifact
hashes, publication-pair lineage, and the Phase 4AC manifest are all validated fail-closed.

The proposal has a caller-supplied evaluation time and positive validity period. Its review
manifest is always published as `UNREVIEWED` with `execution_authorized=false`. Neither the
proposal nor review manifest grants permission to apply a settlement update.

```bash
PYTHONPATH=src python scripts/local/phase4ai_canonicalization_proposal.py \
  --production-db /path/to/production.db \
  --phase4ah-gate-artifact /tmp/phase4ah-gate.json \
  --phase4af-reaudit-artifact /tmp/phase4af-reaudit.json \
  --baseline-phase4af-artifact /tmp/phase4af.json \
  --phase4ag-status-artifact /tmp/phase4ag-status.json \
  --phase4ag-evidence-artifact /tmp/phase4ag-evidence.json \
  --phase4ad-artifact /tmp/phase4ad.json \
  --phase4ae-artifact /tmp/phase4ae.json \
  --history-dir /tmp/phase4ac-history \
  --evaluation-time 2026-08-25T23:00:00Z \
  --valid-for-seconds 3600 \
  --proposal-output /tmp/phase4ai-proposal.json \
  --review-output /tmp/phase4ai-review.json
```

The two files share one deterministic publication pair ID. Both are serialized and `fsync`ed
before atomic publication; existing outputs are refused unless `--replace` is explicit.
