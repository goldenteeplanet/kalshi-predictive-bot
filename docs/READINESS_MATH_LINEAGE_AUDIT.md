# Readiness Math And Lineage Audit

## Crypto Zero-EV Finding

The 2026-08-09 runtime report showed ten current crypto candidates with the same values:

- market midpoint `0.0050`
- best executable ask `0.0100`
- reported side probability `0.01`
- reported expected value `0.000000`

The candidates did not independently converge to zero EV. `crypto_v2` produced continuous
probabilities near `0.0050`, then `_clamp_probability` raised every value below one cent to
`0.01`. Exchange ticks constrain order prices, not model probabilities. The model now keeps
continuous probabilities within `0..1`, so the report exposes the actual edge instead of a
one-cent-floor artifact.

This change does not lower any EV, liquidity, spread, timing, or risk threshold.

## Weather Lineage Finding

The weather gate used `SOURCE_MISSING` for three independent conditions: an ineligible market
window, an unverified Kalshi link, and a missing market snapshot. Runtime rows had fresh weather
source forecasts and features, so `SOURCE_MISSING` was false attribution.

The gate now distinguishes:

- `MARKET_WINDOW_INELIGIBLE`
- `LINK_UNVERIFIED`
- `SNAPSHOT_MISSING`
- `SOURCE_MISSING` only for missing or stale weather-source/feature evidence

Exact-catalog URLs remain non-tradeable until verified. The change improves diagnosis without
weakening the link gate.

## Protocol Math

`kalshi.protocol_math` implements and tests:

- YES/NO reciprocal prices
- fixed-point `price_ranges` and `price_level_structure` tick selection
- cent, decicent, and tapered decicent price validation
- general maker/taker fee formulas rounded up to the nearest centicent
- fee-adjusted per-contract expected value

Rankings now retain gross EV, estimated taker fee, fee-adjusted EV, active tick size, and tick
validity. Existing paper gates still use their established EV and Phase 3N controls; migrating a
gate to fee-adjusted EV requires a separate acceptance change because series-specific fee
multipliers can differ.

Protocol references:

- https://docs.kalshi.com/getting_started/orderbook_responses
- https://docs.kalshi.com/getting_started/fixed_point_migration
- https://docs.kalshi.com/getting_started/fee_rounding
- https://kalshi.com/docs/kalshi-fee-schedule.pdf
