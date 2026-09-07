# Bounded paper-readiness rehearsal

Status: **prepared, not started**. This rehearsal observes fresh public data and
evaluates readiness with execution disabled. It creates no paper, demo, or live
orders. Starting an order-producing paper run requires a later explicit scope.

## Prepared evidence

At 2026-09-07 23:24 UTC, five unauthenticated GET requests returned HTTP 200:
the open `KXTEMPNYCH` market list, series metadata, and three depth-five books.
The public endpoints are documented in
[Kalshi's market-data guide](https://docs.kalshi.com/getting_started/quick_start_market_data).
The capture contains 10 listed markets and these three sampled tickers:

| Ticker | YES bid levels | NO bid levels |
| --- | ---: | ---: |
| KXTEMPNYCH-26SEP0720-T76.99 | 0 | 5 |
| KXTEMPNYCH-26SEP0720-T75.99 | 0 | 5 |
| KXTEMPNYCH-26SEP0720-T74.99 | 4 | 5 |

All three reported active status and close time `2026-09-08T00:00:00Z`.
Two books lack YES bids; an HTTP success is not proof of executable liquidity.
No weather forecast, settlement-link certification, or profitable candidate was
established. These observations are preparation evidence, not an activation signal.
Refresh the market list and books before any rehearsal; never reuse closed tickers.

The unsynced local run directory is:
`C:/Users/user1/AppData/Local/CodexPaperRehearsals/20260907T232421Z`.
It contains raw responses, `preparation.json`, `table-counts.json`, `paper.db`, and
`paper.empty-baseline.db`. The application schema has 164 tables, all empty;
SQLite integrity check passed. Database SHA-256:
`3d26fbf80aa40123d78a4366f7d2be92a2ad3b90891981386388864a4633fc8d`.
An earlier workspace capture is archival only; use the unsynced directory above.
The preparation code revision and response hashes are in
[paper-preparation.json](paper-preparation.json).

## Entry criteria

1. All hosted PR checks pass for the exact intended code revision. Record the SHA
   and use a separate clean checkout; do not pull later active-task work into it.
2. Create a fresh copy of the empty baseline in a new unsynced run directory.
   Resolve both `DATABASE_URL` and `KALSHI_DB_URL` to that same absolute SQLite
   path. Refuse any existing runtime database, linked path, or shared writer.
   Compare schema against the chosen revision before importing observations.
3. Use a dedicated process with an allowlisted environment, no `.env` loading,
   no account credentials, no authenticated WebSocket, and no service startup.
   Validate the effective settings, not just the exported environment values:

   | Setting | Required value |
   | --- | --- |
   | EXECUTION_ENABLED / AUTOPILOT_ENABLED | false |
   | EXECUTION_DRY_RUN / AUTOPILOT_DRY_RUN | true |
   | EXECUTION_GATEWAY_MODE | disabled |
   | PAPER_ORDER_CREATION_ENABLED | false |
   | PAPER_ORDER_KILL_SWITCH | true |
   | DYNAMIC_POSITION_SIZING_MODE / ADVANCED_RISK_ENGINE_MODE | disabled |
   | KALSHI_WEBSOCKET_ENABLED | false |

4. Rediscover at most three exact tickers in the supported `KXTEMPNYCH` scope.
   Require active status and at least 15 minutes until close for this short
   rehearsal. Received market/book data must be no older than 60 seconds when
   evaluated; retain provider timestamps separately from receipt timestamps.
   Do not treat receipt time as proof that an underlying quote or forecast is fresh.

## Bounded procedure for the next task

- Maximum 10 minutes, five sequential observation cycles, at least 60 seconds
  between cycle starts, three tickers, depth five, and 25 public GET requests.
  Each request has a 15-second timeout and one-MB response limit. Refuse redirects,
  authentication, non-GET methods, and endpoints outside public series/markets/books.
  Stop on the first request failure; no retries or scope expansion in this run.
- Write raw evidence and imported market/snapshot rows only to the new run's
  directory/database. Audit the importer before running it. No order/fill/position
  writes, `paper-run`, activation token, scheduler, deployment, or service command.
- Record timestamps, status, market close, book depth, spread, and source lineage
  for each cycle. Missing sides or missing source evidence must produce an explicit
  blocked result. Do not fill missing quotes with last prices or invented liquidity.
- Evaluate existing readiness gates without lowering EV, confidence, spread,
  liquidity, settlement, or risk thresholds. This empty database has no certified
  forecast lineage or 24-cycle healthy soak. Missing prerequisites should fail
  closed; five observation cycles do not satisfy the GH-4 activation requirement.
  Authenticated WebSocket evidence is outside this rehearsal's scope.
- Verify zero rows in paper orders, fills, positions, P&L, and autopilot trade
  tables before and after every cycle. Preserve settings, request logs, row-count
  deltas, SQLite integrity results, code SHA, and a final `PASS` or `BLOCKED` report.
  `PASS` means this bounded observation process stayed within its limits; it does
  not authorize trading or certify strategy performance.

## Stop conditions and rollback

Stop immediately for a changed safety flag, unexpected database path/writer,
credential loading, prohibited request, any order-related row, stale/closed market,
schema or lineage mismatch, integrity failure, request error, or exhausted budget.
An unmet readiness gate is a recorded blocker, never a reason to weaken the gate.

Terminate only the rehearsal child process, close its connections, and retain the
entire failed run plus logs for inspection. Verify that execution remains disabled
and that no service was started. Do not alter any existing runtime or other task's
worktree. After confirming the failed process is gone, make a new run directory
from the verified empty baseline for a future attempt; never overwrite the failed
database or delete its ledger evidence. No shared runtime rollback is needed because
this procedure never changes the shared runtime.
