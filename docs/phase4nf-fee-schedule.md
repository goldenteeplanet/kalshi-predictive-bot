# Phase 4NF — Fee Versioning and Worst-Case Transaction Costs

## Outcome

Phase 4NF selects only the fee schedule both publicly published and effective at simulated order
time. It models maker/taker rates, price and quantity, ceiling-rounding, minimums, caps, rebates,
surcharges, partial fills, cancel/replace fees, and settlement fees.

## Cost boundary

Overlaps, gaps, future publication, retroactive revisions, malformed negative values, invalid fills,
and rebates exceeding charges refuse. Ambiguous liquidity status uses maker for a labeled baseline
and the more expensive verified role for readiness. Worst-case costs apply minimums per partial fill.

## Reproducible evidence

- focused Phase 4NE–4NF suite: 20 passed
- fee-schedule SHA-256: `cf0ca1fa35c23064b84e79bd05f13f4416bac93764eeb6eef5787f31d6056398`
- fee-envelope SHA-256: `46d2c76c6c770725938d23b546fcbd1f5560d0c1101ebc940117c0fcb5983901`
- baseline / worst-case verified fees: `0.110` / `0.260`
- simple flat fee: `0.010`
- simple-model P&L inflation: `0.250`
- worst-case verified net P&L: `-0.060`; readiness: `REFUSE`
- comparison SHA-256: `775a0ed507cab35446304eed5c8073f54ed4f8c5f0d1a7073a5be267212dbd55`

## Safety and removal

The model is offline, deterministic, and non-persistent, with no order, network, runtime, paper,
demo, live, or autopilot capability. Remove the three phase files to roll back.

## Next phase

Phase 4NG — Liquidity-capacity curve and market-impact stress proof.
