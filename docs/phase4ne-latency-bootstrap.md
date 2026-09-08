# Phase 4NE — Latency Bootstrap and Tail Execution Risk

## Outcome

Phase 4NE jointly block-bootstraps decision-to-send, acknowledgment, market-data age, processing
pause, scheduler jitter, reconnect delay, regime, and censoring observations. It preserves observed
component correlations and propagates every sampled path through Phase 4ND's pessimistic replay.

## Tail boundary

The report includes p50, p90, p95, p99, and worst latency plus fill, P&L, adverse-selection,
stale-book, rejection, and closure distributions. Timeouts and missing acknowledgments are retained
as right-censored tail observations. Sparse samples and unacceptable p99 economics refuse readiness.

## Reproducible evidence

- focused Phase 4ND–4NE suite: 20 passed
- source observations / bootstrap paths: 30 / 100
- latency p50 / p90 / p95 / p99 / worst milliseconds: `65 / 588 / 588 / 588 / 588`
- stale-book rejections: 18
- total replay rejections: 18
- p99-path net P&L: `0`
- bootstrap SHA-256: `8e624e8b92ffaf07de3027e9f7c71c6a182fd31e52f22e1bf9decc94c2eed233`

## Safety and removal

All resampling and replay is deterministic, offline, and non-persistent. It cannot submit or create
orders, access a network, modify runtime state, or enable paper, demo, live, or autopilot execution.
Remove the three phase files to roll back.

## Next phase

Phase 4NF — Fee-schedule versioning and worst-case transaction-cost proof.
