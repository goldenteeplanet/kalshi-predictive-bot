# Phase 3M Dynamic Position Sizing

Phase 3M replaces the fixed paper-order quantity with one deterministic sizing decision at the paper order boundary.

## Inspected Quantity Path

- Signal and forecast filtering: `src/kalshi_predictor/paper/strategy.py`
- Persisted paper order boundary: `src/kalshi_predictor/paper/ledger.py`
- Immediate simulated fills: `src/kalshi_predictor/paper/simulator.py`
- UI paper-order path: `src/kalshi_predictor/ui/service.py`
- Backtest fixed-size replay path: `src/kalshi_predictor/backtesting/strategy.py`

The previous authoritative size source was `settings.paper_max_order_quantity`, defaulting to `1`.

## Sizing Formula

The sizer normalizes explicit 0-100 repository scores to 0-1 inputs:

- confidence score from existing model-confidence/opportunity scoring
- opportunity score from existing payout-adjusted opportunity scoring
- liquidity score from existing volume/open-interest/liquidity scoring
- drawdown health from paper daily P&L versus the existing drawdown guardrail
- historical accuracy from paper orders with `settlement.settled_at < decision_timestamp`

Composite score:

```text
confidence * 0.35
+ opportunity * 0.25
+ liquidity * 0.15
+ adjusted_historical_accuracy * 0.15
+ drawdown_health * 0.10
```

Tier proposal:

- Low: 1 contract
- Medium: 3 contracts
- High: 5 contracts

Cap order:

```text
min(
  proposed_contracts,
  liquidity_cap,
  drawdown_cap,
  history_cap,
  external_risk_cap,
  margin_cap,
  portfolio_cap,
  live_max_contracts,
  global_max_contracts
)
```

The result is bucketed downward to `0`, `1`, `3`, or `5`.

## Modes

- `disabled`: calculates and logs, but executes 1 unless an existing hard cap blocks.
- `shadow`: calculates and logs the live candidate, but executes 1 unless an existing hard cap blocks.
- `live`: executes the capped dynamic quantity.

Rollback is configuration-only:

```bash
DYNAMIC_POSITION_SIZING_MODE=disabled
DYNAMIC_POSITION_SIZING_LIVE_MAX_CONTRACTS=1
```

## Missing Inputs

This repository is paper/demo only. It has no broker buying-power, margin, live account-risk, stop-distance, or production order-management subsystem. For that reason:

- `DYNAMIC_POSITION_SIZING_EXTERNAL_RISK_CAP` defaults to unset.
- In live mode, a missing external risk cap limits execution to 1 and records `MISSING_EXTERNAL_RISK_CAP`.
- Optional margin and portfolio caps can be supplied by configuration, but no broker data is fabricated.

## Persistence

Each sizing decision is stored in `position_sizing_decisions` with:

- tier, proposed size, live candidate, executed quantity
- factor scores and weights
- caps and limiting factors
- reason codes
- forecast/trade intent correlation
- `paper_order_id` once an order is created

## Rollout

Stage 0:

```bash
DYNAMIC_POSITION_SIZING_MODE=disabled
DYNAMIC_POSITION_SIZING_LIVE_MAX_CONTRACTS=1
```

Stage 1:

```bash
DYNAMIC_POSITION_SIZING_MODE=shadow
DYNAMIC_POSITION_SIZING_LIVE_MAX_CONTRACTS=1
```

Stage 2:

```bash
DYNAMIC_POSITION_SIZING_MODE=live
DYNAMIC_POSITION_SIZING_LIVE_MAX_CONTRACTS=3
DYNAMIC_POSITION_SIZING_EXTERNAL_RISK_CAP=3
```

Stage 3:

```bash
DYNAMIC_POSITION_SIZING_MODE=live
DYNAMIC_POSITION_SIZING_LIVE_MAX_CONTRACTS=5
DYNAMIC_POSITION_SIZING_EXTERNAL_RISK_CAP=5
```

Do not enable live sizes above 1 until the shadow decision logs show acceptable missing-data, cap, slippage, and drawdown behavior.
