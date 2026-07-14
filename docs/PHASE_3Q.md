# Phase 3Q Auto Feature Discovery

Phase 3Q is a read-only research subsystem. It discovers, rejects, ranks, and
reports candidate features from Phase 3O memory. It does not retrain models,
promote features, tune Phase 3M sizing, tune Phase 3N risk, or submit orders.

## Commands

```bash
kalshi-bot feature-discovery-status
kalshi-bot feature-discovery-run \
  --run-type ON_DEMAND \
  --training-as-of 2026-06-23T02:00:00-05:00 \
  --output reports/feature_discovery_report.md \
  --json-output reports/feature_discovery_report.json
kalshi-bot feature-discovery-report
kalshi-bot feature-experiment-export \
  --evaluation-id <feature_evaluation_id> \
  --human-approval-reference <ticket-or-note> \
  --output reports/feature_experiment_spec.json
```

## Authoritative Flow

```text
Phase 3O market_memory / forecast_memory / trade_memory
  -> frozen training_as_of cutoff
  -> point-in-time availability and label-finality checks
  -> bounded candidate grammar
  -> leakage and quality screening
  -> purged walk-forward evidence
  -> paired baseline/candidate comparison
  -> q-value adjustment and conservative lifecycle status
  -> append-only Phase 3Q scorecards and Markdown/JSON reports
```

## Outcome And Metrics

- Primary trade outcome: `net_profitable_after_costs`, using `trade_memory.net_pnl > 0`
  only when `total_cost` is present and the outcome was finalized before
  `training_as_of`.
- Forecast-only outcome: `forecast_direction_profitable_proxy`; this can support
  watchlist evidence but cannot prove net P&L.
- Metrics include baseline outcome rate, candidate top-vs-bottom paired delta,
  net P&L economic effect when available, fold stability, q-value, and composite
  research score.

## Candidate Grammar And Leakage Controls

Allowed source fields are explicitly allowlisted in
`kalshi_predictor.feature_discovery.contracts.ALLOWED_FEATURE_SOURCES`.
Forbidden lineage/source tokens include outcome, settlement, realized P&L, exit
price, future excursions, Phase 3P findings, and target labels.

Expressions are canonicalized so equivalent expressions share deterministic
candidate IDs. Centered windows, unknown source fields, missing division zero
policy, and excessive interaction depth are rejected.

## Human Review Boundary

Recommendations are proposals only. Any action other than `NO_ACTION` is marked
`HUMAN_REVIEW_REQUIRED`. Exporting an offline experiment spec requires an
explicit human approval reference and does not alter production configuration.

## Rollout

1. Run `feature-discovery-run` in shadow mode on a small cutoff.
2. Review rejected candidates and unavailable-source disclosures.
3. Backfill representative sessions only after Phase 3O coverage is healthy.
4. Add `feature-discovery-nightly` to external scheduling only after review.
5. Keep production model, Phase 3M, Phase 3N, and execution paths unchanged.

## Rollback

Stop running Phase 3Q commands or set `PHASE_3Q_FEATURE_DISCOVERY_ENABLED=false`.
Persisted Phase 3Q research records remain queryable. Trading behavior is
unchanged.

## Missing Or Deferred Inputs

- Dedicated production feature registry and promotion workflow.
- Versioned counterfactual simulator and fill model.
- Authoritative exchange fee schedule beyond stored `trade_memory.total_cost`.
- Protected holdout registry outside the Phase 3Q access log table.
- Multi-year archive replay and large-volume performance validation.
