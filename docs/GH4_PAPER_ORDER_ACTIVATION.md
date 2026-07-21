# GH-4 Operator-Approved Paper Order Activation

GH-4 enables simulated paper-order creation only after GH-3 proves that the live-data
decision loop is healthy. It never enables Kalshi exchange execution, demo execution,
or autopilot.

## Preflight

Run this read-only command on the cloud host:

```bash
kalshi-bot gh4-paper-activation-preflight \
  --output-dir reports/phase_gh4 \
  --gh2-report-path /var/lib/kalshi-bot-gh2/reports/gh2_active_candidate_refresh.json \
  --gh2-history-path /var/lib/kalshi-bot-gh2/reports/gh2_paper_only_soak_history.jsonl \
  --gh1-status-path /var/lib/kalshi-bot-gh1/watch/status.json
```

The command performs no database writes and creates no paper or exchange orders.

## Go Gates

- GH-3 has at least 24 consecutive healthy cycles.
- A paper-ready candidate appeared during the required soak window.
- A paper-ready candidate is available at activation time.
- The latest GH-2 cycle is healthy and has no source or stage errors.
- Kalshi WebSocket, Coinbase, and NOAA weather evidence is current.
- The soak created zero paper orders.
- `EXECUTION_ENABLED=false`.
- `AUTOPILOT_ENABLED=false`.
- The preflight status is `READY_FOR_OPERATOR_APPROVAL`.

Any failed gate is a no-go. Do not lower the edge, liquidity, freshness, or risk
thresholds merely to make a candidate pass.

## Activation

Activation requires all three controls in the same operator session:

```bash
export PAPER_ORDER_CREATION_ENABLED=true
export PAPER_ORDER_KILL_SWITCH=false
export EXECUTION_ENABLED=false
export AUTOPILOT_ENABLED=false

kalshi-bot paper-run \
  --model-name crypto_v2 \
  --approval-token I_APPROVE_GH4_PAPER_ORDER_CREATION \
  --gh2-report-path /var/lib/kalshi-bot-gh2/reports/gh2_active_candidate_refresh.json \
  --gh2-history-path /var/lib/kalshi-bot-gh2/reports/gh2_paper_only_soak_history.jsonl \
  --gh1-status-path /var/lib/kalshi-bot-gh1/watch/status.json
```

Start with one manual bounded run. Do not install a recurring paper-order service until
the first run's orders, fills, positions, P&L, limits, and reconciliation are verified.

## Verification

```bash
kalshi-bot paper-summary --output reports/paper_trading.md
kalshi-bot paper-pnl
kalshi-bot paper-settlement-doctor \
  --output-dir reports/paper_settlement_reconciliation
kalshi-bot db-writer-monitor --json
```

Verify that every order is simulated, every fill references its paper order, position
quantities match fills, risk limits cap quantity and exposure, and settled P&L matches
the exact ticker settlement.

## Kill Switch And Rollback

Set the kill switch first:

```bash
export PAPER_ORDER_KILL_SWITCH=true
export PAPER_ORDER_CREATION_ENABLED=false
```

Then stop any future GH-4 paper-order timer or service, if one has been explicitly
installed, and rerun the preflight. Do not delete paper ledger rows during incident
review. `EXECUTION_ENABLED` and `AUTOPILOT_ENABLED` must remain false throughout
rollback.
