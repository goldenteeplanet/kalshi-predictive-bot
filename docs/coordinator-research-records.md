# Coordinator research assessments

The guarded local coordinator persists a `COORDINATOR_RESEARCH_ASSESSMENT_V1`
checkpoint in the same SQLite transaction as each new qualification attempt.
Its key is `release-research-v1:<decision_id>`. Replays compare immutable content;
failed transactions leave neither a qualification nor a research assessment.
Existing-order reconciliation and exhausted-capacity exits retain their earlier
behavior and do not create a new assessment. This is not a general research
ingestion API or a backfill of historical experiments.

The assessment references the exact qualification checkpoint and its SHA-256.
It records `RULE_UNCERTIFIED`, `BOOK_INVALID`, or `COST_UNKNOWN`, in that priority
order, while retaining all simultaneous research and qualification blockers.
The original qualification value is explicitly provisional: its arithmetic gate
and artifact provenance do not certify fee applicability, slippage, uncertainty,
or independent model skill. Full net EV remains null. This adapter cannot emit
`POSITIVE_NET_EV` or `NEGATIVE_NET_EV_RESEARCH` until a reviewed full-cost evidence
validator is integrated. A hash establishes content identity, not external truth.

The read-only `/api/paper-live` and `/paper-live` views expose historical
assessment counts and the latest assessment separately from eligible shadows
and paper positions. Readers validate the linked checkpoint and derived content;
missing or altered lineage makes the dashboard unverified. These records do not
change qualification gates, activate paper, certify current freshness, alter
frozen forecasts, or write an `overnight_shadow` row for a blocked candidate.
