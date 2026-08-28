# One-contract weather paper activation — 2026-08-28

## Outcome

- Authorized ticker: `KXRAINAUSM-26AUG-1`
- Exact approval: `AUTHORIZE ONE PAPER CONTRACT KXRAINAUSM-26AUG-1`
- Result: `ONE_CONTRACT_PAPER_ORDER_CREATED`
- Paper order: `205`
- Immediate simulated fill: `205`
- Forecast/snapshot pair: `forecast:573256:snapshot:457115`
- Quantity: `1`
- Side: `BUY_YES`
- Fill price: `0.0500`
- Live execution: blocked

## Readiness evidence

- The weather paper gate was `WEATHER_PAPER_READY` for only the authorized ticker.
- The coherent preflight proposed one contract and Phase 3N returned `ALLOW` with no hard blocks.
- The pair key was retained in the idempotency state.
- The fast-preflight soak completed with at least three consecutive healthy cycles.
- The activation was synchronized to the post-preflight gate before the order transaction.

## Database invariants

Before activation:

- `paper_orders`: count/max ID `204/204`
- `paper_fills`: count/max ID `204/204`
- `position_sizing_decisions`: count/max ID `239/239`
- `advanced_risk_decisions`: count/max ID `239/239`

After activation:

- `paper_orders`: count/max ID `205/205`
- `paper_fills`: count/max ID `205/205`
- `position_sizing_decisions`: count/max ID `240/240`
- `advanced_risk_decisions`: count/max ID `240/240`

Order 205 is `FILLED`, quantity one, and links to sizing decision 240 and risk decision 240. Risk decision 240 is `ALLOW`, shadow mode, with no hard blocks. Repeating the exact activation returned `IDEMPOTENT_EXISTING_ORDER` for order/fill 205 and did not change any count.

## Fail-closed state after the transaction

- `execution_enabled=False`
- `execution_dry_run=True`
- `execution_gateway_mode=disabled`
- `autopilot_enabled=False`
- `autopilot_dry_run=True`
- `paper_order_creation_enabled=False`
- `paper_order_kill_switch=True`
- `paper_max_order_quantity=1`
- `dynamic_position_sizing_mode=disabled`
- `advanced_risk_engine_mode=disabled`
- Scheduler service: active
- Read-only UI service: active

The activation command enabled paper creation only inside its explicit transaction settings. No exchange, demo, or live order path was enabled.

## Recovery and rollback evidence

- Failed activation attempts were rolled back automatically and left order/fill counts at 204.
- Runtime file backups are under `/home/james/kalshi-local-runtime/paper-readiness-backup-20260828T1625Z`.
- The `Dejoia-Kalshi-WSL-Keepalive` Windows task keeps WSL available for the scheduler.
- The `trading-bot-wsl-watchdog` heartbeat checks WSL, both services, and fail-closed execution settings every ten minutes.
- No repository push was performed.
