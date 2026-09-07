# Phase 4AH timestamp evidence re-audit gate

Phase 4AH validates the complete Phase 4AG status/evidence pair and supplies only that validated
evidence to the existing Phase 4AF implementation. Timestamp precedence, normalization, freshness,
and conflict rules therefore remain defined in one place.

The gate compares baseline and re-audit rows by `(capture_id, ticker)`. It records evidence-added,
unchanged, canonical-appeared, conflict, ambiguity, stale, missing, lineage, evaluation, added, and
removed transitions. Gate precedence is unexpected condition, lineage/cohort change, conflict,
staleness, fully ready, partially ready, waiting, then empty cohort.

`READY_FOR_CANONICALIZATION_PROPOSAL` authorizes only construction of a future proposal artifact.
It does not authorize a settlement write. Phase 4AH contains no exchange, service-control, trading,
or database-write interface.

```bash
PYTHONPATH=src python scripts/local/phase4ah_timestamp_readiness_gate.py \
  --baseline-phase4af-artifact /tmp/phase4af.json \
  --phase4ag-status-artifact /tmp/phase4ag-status.json \
  --phase4ag-evidence-artifact /tmp/phase4ag-evidence.json \
  --phase4ad-artifact /tmp/phase4ad.json \
  --phase4ae-artifact /tmp/phase4ae.json \
  --history-dir /tmp/phase4ac-history \
  --production-db /path/to/production.db \
  --evaluation-time 2026-08-25T23:00:00Z \
  --freshness-seconds 1800 \
  --reaudit-output /tmp/phase4af-reaudit.json \
  --gate-output /tmp/phase4ah-gate.json
```

The re-audit and gate carry one deterministic publication pair ID. Both files are serialized and
`fsync`ed before publication; existing outputs are refused without explicit `--replace`.
