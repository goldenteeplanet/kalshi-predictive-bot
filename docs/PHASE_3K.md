# Phase 3K: Market Microstructure Engine

Phase 3K adds a read-only market microstructure layer. It analyzes stored market snapshots and
stored orderbook JSON to turn short-term market behavior into features, signals, reports, and
paper/demo forecast context.

It does not place live trades, does not authenticate with Kalshi, and does not make live network
calls while building features.

## What It Tracks

- Spread changes: tightening, widening, spikes, and normalization.
- Liquidity changes: improving liquidity, drying liquidity, and sudden liquidity spikes.
- Orderbook imbalance: YES pressure, NO pressure, balanced books, and pressure flips.
- Price dislocations: market price versus `ensemble_v2`, plus cross-model disagreement.
- Late moves: price velocity, acceleration, liquidity change, and time-to-close context.
- Possible informed flow: a cautious heuristic from movement, spread, liquidity, imbalance, late
  timing, and dislocation.

## Tables

- `microstructure_features`: one feature row per ticker/lookback build.
- `microstructure_events`: detected spread, liquidity, imbalance, dislocation, late-move, and
  possible informed-flow events.
- `microstructure_signals`: Phase 3K signal rows linked to the Signal Marketplace.
- `orderbook_depth_snapshots`: captured top-of-book depth summaries derived from stored orderbook
  data.

## CLI Flow

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot build-microstructure-features --lookback-minutes 60
kalshi-bot forecast --model microstructure_v1
kalshi-bot microstructure-report --output reports/microstructure_report.md
kalshi-bot microstructure-opportunities --model-name microstructure_v1 --limit 20 --output reports/microstructure_opportunities.md
kalshi-bot scheduler-plan --profile microstructure-watch
kalshi-bot ui
```

`microstructure_v1` starts from the stored market midpoint and applies a bounded adjustment. It
skips markets that do not have enough microstructure snapshots or do not have usable market prices.

## UI

The UI adds:

- `/microstructure`: a dashboard for recent features, events, and report links.
- `/microstructure/{ticker}`: ticker detail with spread, liquidity, imbalance, events, signals, and
  a research explanation.
- Opportunity card microstructure context: spread trend, liquidity trend, orderbook pressure, late
  move warning, and possible informed-flow warning.

## Safety Notes

Possible informed flow is only a heuristic. It can be caused by real information, routine liquidity
changes, stale orderbooks, thin markets, or noise. Treat it as a review flag, not proof.

All outputs remain paper/demo only. Phase 3K does not add production execution, real-money order
routing, private account access, or live trading.
