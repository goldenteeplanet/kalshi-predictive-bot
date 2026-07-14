# Phase 3P Self-Evaluation Engine

Phase 3P is a read-only evaluation and reporting subsystem. It reads Phase 3O
`market_memory`, `forecast_memory`, and `trade_memory` records at a frozen
`evaluation_as_of` cutoff, then writes structured metrics, findings, and a
deterministic Markdown journal.

## Commands

```bash
kalshi-bot self-evaluate \
  --session-date 2026-06-22 \
  --evaluation-as-of 2026-06-23T02:00:00-05:00 \
  --output reports/self_evaluation_journal.md \
  --json-output reports/self_evaluation_journal.json
```

The command does not retrain, promote, configure, size, approve, route, open, or
close trades. Recommendations are always marked `HUMAN_REVIEW_REQUIRED`.

## Authoritative Data Flow

```text
Phase 3O market_memory
Phase 3O forecast_memory
Phase 3O trade_memory
  -> session/cutoff adapter
  -> coverage and finality validator
  -> deterministic metric records
  -> worked/failed/changed finding detector
  -> structured JSON journal
  -> deterministic Markdown journal
```

## Metric Catalog

- `coverage.forecasts.eligible`: count of latest forecast records generated in the session.
- `coverage.trades.eligible`: count of latest trade records linked to the session.
- `coverage.market_snapshot_link_rate`: forecasts with Phase 3O market link divided by eligible forecasts.
- `forecast.direction_accuracy`: mean of `direction_correct` over finalized forecasts.
- `forecast.brier_score`: mean squared probability error for finalized binary forecasts.
- `forecast.no_trade_count`: count of forecasts finalized as no-trade decisions.
- `forecast.risk_blocked_count`: count of risk-blocked forecasts or Phase 3N block actions.
- `opportunity.score.mean`: mean opportunity score across eligible forecasts.
- `phase3m.tier.count`: Phase 3M tier distribution.
- `phase3n.action.count`: Phase 3N action distribution.
- `phase3n.reason.count`: Phase 3N reason-code distribution.
- `trade.*`: execution-mode-separated finalized trade counts, P&L, cost, and win-rate metrics.
- `model.version.unique_count`: deterministic model-version change detector.
- `feature_schema.version.unique_count`: deterministic feature-schema change detector.
- `data_quality.forecast_lineage_missing`: forecasts missing model or feature lineage.

## Baseline Rules

Baselines use prior persisted Phase 3P metric records with matching metric name
and cohort. The current session is excluded. If baseline sample size is below
`PHASE_3P_MINIMUM_BASELINE_SAMPLE`, the baseline is disclosed as fallback and is
not used for supported worked/failed claims.

## Rollout

1. Keep `PHASE_3P_MODE=shadow`.
2. Backfill representative sessions manually with `kalshi-bot self-evaluate`.
3. Review unsupported claims and tune policy thresholds through versioned config.
4. Add `self-evaluation-nightly` to the scheduler only after review.
5. Move to `PHASE_3P_MODE=production_journal` only after historical replay.

## Rollback

Set `PHASE_3P_MODE=disabled` or stop running `kalshi-bot self-evaluate`.
Generated runs, metrics, findings, and journals remain queryable. Phase 3O
capture, Phase 3M sizing, Phase 3N risk, execution, and settlement are untouched.

## Missing Or Deferred Inputs

- No authoritative exchange holiday/early-close calendar module was found; the
  implementation uses a full local calendar day and records this caveat.
- No versioned counterfactual simulator/fill model is available; counterfactual
  metrics are not produced.
- Publication/alerting telemetry is not made the sole safety path and is not
  enabled by Phase 3P.
- Model deployment records are inferred from Phase 3O forecast lineage fields;
  dedicated deployment registry integration remains deferred.
