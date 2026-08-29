# Phase 4NH — Portfolio Correlation and Joint-Tail Loss

## Outcome

Phase 4NH aggregates existing positions and proposed simulated trades by event, underlying,
geography, time window, source, model, and latent factor. It calculates deterministic scenario P&L,
factor concentration, covariance stress, marginal risk, expected shortfall, simultaneous worst loss,
drawdown consumption, and remaining capacity.

## Portfolio boundary

Every proposed trade must carry pessimistic-fill and worst-case-fee proof. Duplicate orders,
duplicated economic exposure, stale or missing portfolio state, correlated losses, common-source
exposure, settlement mismatch, and hedge failure remain explicit. Profitable standalone trades can
still fail portfolio readiness, and no result authorizes an order.

## Reproducible evidence

- focused Phase 4NG–4NH suite: 20 passed
- individually profitable correlated-trade audit SHA-256: `eafdfe7b9f9d1beb59eef97187c0dcb1411ef6b76fa40e5ad92ff09803815211`
- joint worst-case / expected-shortfall P&L: `-6.05` / `-6.05`
- perfect-correlation loss: `-7`
- concentration: `1`
- drawdown consumption / remaining capacity: `7` / `-5`
- readiness: `REFUSE` for both joint-loss and concentration breaches

## Safety and removal

The model is offline and non-persistent, with no order, network, runtime, paper, demo, live, or
autopilot capability. Remove the three phase files to roll back.

## Next phase

Phase 4NI — Scenario-generation coverage and rare-joint-event mutation proof.
