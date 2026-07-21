# GH-3 Paper-Ready Blocker and Source Audit

Read-only cloud evidence captured on 2026-07-21. No paper, demo, or exchange orders
were created.

## Soak State

- Consecutive healthy cycles: 11 of 24 at the audit snapshot.
- Current paper-ready candidates: 0.
- Paper-ready candidate seen in required window: no.
- Current positive-EV rows: 3.
- GH-2 cycle errors: 0.

## Positive-EV Blockers

The three positive-EV rows were fresh. Staleness was not their immediate blocker.

| Ticker | EV | Paper threshold | Liquidity score | Preflight blockers |
| --- | ---: | ---: | ---: | --- |
| KXXRP-26JUL2110-T1.7399 | 0.5 cents | 5.0 cents | 0.00 | LOW_EDGE, LIQUIDITY_ZERO, RISK_MISSING |
| KXBTC-26JUL2110-T73299.99 | 0.2 cents | 5.0 cents | 0.00 | LOW_EDGE, LIQUIDITY_ZERO, RISK_MISSING |
| KXETH-26JUL2110-T2594.99 | 0.2 cents | 5.0 cents | 0.00 | LOW_EDGE, LIQUIDITY_ZERO, RISK_MISSING |

Additional active-window gaps included two stale snapshots, two missing snapshots,
and three current forecasts without rankings. Those rows were not the three current
positive-EV rows.

The correct response is to continue collecting executable books and current risk
evidence. Do not lower the edge, liquidity, or risk thresholds to force a paper-ready
candidate.

## Source Health

- Kalshi WebSocket state: STREAMING.
- Messages seen: 2,988.
- Snapshots staged: 2,779.
- Reconnects: 3, including transient HTTP 503 responses; the service recovered.
- Discovery failures: 0.
- Coinbase symbols imported per bounded cycle: BTC, DOGE, ETH, SOL, and XRP.
- Weather decision refresh: four current features and two forecasts at the snapshot.

## Engineering Findings

1. The WebSocket watch already reconnects with bounded exponential backoff, but its
   status did not expose consecutive failures or last successful discovery/message/
   snapshot timestamps. GH-4 preparation adds those fields.
2. Coinbase filesystem staging had no immediate retry around an empty transient
   result. GH-4 preparation adds three bounded attempts with backoff.
3. NOAA fetches had no immediate retry. GH-4 preparation adds three bounded attempts
   with backoff.
4. The crypto quote drain scanned its `drained/` archive and re-imported five old
   files alongside five new files. GH-4 preparation excludes the archive so each
   cycle drains only pending staged files.

## Activation Decision

GH-4 remains a no-go until all soak gates pass and a currently executable paper-ready
candidate exists. Live execution and autopilot remain disabled.
