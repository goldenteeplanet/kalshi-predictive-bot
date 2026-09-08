# Phase 4NG — Liquidity Capacity and Market Impact

## Outcome

Phase 4NG integrates executable ask depth by level for increasing requested sizes. It reports
optimistic, central, and pessimistic fill, marginal price, average price, fee, and expected-P&L
curves. Readiness uses only fully filled, positive-edge points on the pessimistic curve.

## Capacity boundary

The pessimistic model haircuts withdrawing or spoof-like depth, preserves queue-ahead and correlated
demand, adds nonlinear self-impact, honors sweep price caps and tick size, and never assumes hidden
replenishment. Zero, stale, crossed, locked, duplicate, off-tick, and non-monotonic books refuse.

## Reproducible evidence

- focused Phase 4NF–4NG suite: 20 passed
- book SHA-256: `f270817a34ae895192642f6d9df6bd9eb88d11bb9774d5d7fd43424aed5fce53`
- capacity-curve SHA-256: `40993fccfec4487fd412438f9e18d6a8c2f6738945adabf42b74dfb8b13ee0c3`
- pessimistic fully filled profitable capacity: 20 contracts
- 30-contract request pessimistic fill: `21.50` contracts; therefore refused
- size assessment SHA-256: `268db980a78805ebac4d569e03252647638ff3026803d514fd85e174290b8835`
- fixed-size comparison SHA-256: `480c82c503c7a65392f72ff32ba2dcfa00c4ea1a614031311452340ad1147cc6`

## Safety and removal

All depth integration is offline and non-persistent, with no order, network, runtime, paper, demo,
live, or autopilot capability. Remove the three phase files to roll back.

## Next phase

Phase 4NH — Portfolio-level correlated exposure and joint-tail loss proof.
