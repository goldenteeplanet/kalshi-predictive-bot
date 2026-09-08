# Phase 4ND — Microstructure Replay and Pessimistic Fill Envelope

## Outcome

Phase 4ND replays sequence-ordered book, trade, queue-reset, and closure events using only the book
available at simulated arrival time. It produces optimistic, central, and pessimistic fill and P&L
envelopes, while readiness is explicitly bound only to the pessimistic envelope.

## Fill boundary

The replay models spread crossing, visible depth, queue-ahead volume, burst traffic, partial fills,
latency, delayed acknowledgments, cancel/replace priority loss, fees, tick size, closure, and
post-fill adverse selection. Stale, crossed, ambiguous, missing-sequence, and hidden-liquidity inputs
refuse rather than receiving invented fills.

## Reproducible evidence

- focused Phase 4NC–4ND suite: 20 passed
- replay SHA-256: `6d8285b7adb8e46d3a1e8c7731185c9d08a2b72df5c8e58adb97e876121999fe`
- optimistic/central/pessimistic fills: `5` / `4` / `3`
- pessimistic net P&L: `1.44`
- simple full-fill P&L inflation: `0.96`
- comparison SHA-256: `576c1dc96586e5635549d98575b35e3b4c2cc822673e0b83be7f632aefddf8bf`

## Safety and removal

All events and orders are plain copied data. The replay cannot submit or create orders, persist
state, access a network, modify runtime controls, or enable paper, demo, live, or autopilot modes.
Remove the three phase files to roll back.

## Next phase

Phase 4NE — Latency-distribution bootstrap and tail-execution-risk proof.
