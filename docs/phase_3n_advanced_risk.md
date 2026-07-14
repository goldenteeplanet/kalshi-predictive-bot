# Phase 3N Advanced Risk Engine

## Repository Findings

- Phase 3M lives in `src/kalshi_predictor/position_sizing/`. Its authoritative order boundary is `PositionSizingDecision.executed_contracts`, because disabled and shadow modes intentionally preserve the prior one-contract paper behavior.
- New paper positions are created through `paper.strategy.generate_paper_decisions()` and `paper.ledger.create_paper_order()`. Both paths call `ensure_paper_decision_sized()`, so Phase 3N is integrated there once and is idempotent through `advanced_risk_decision_id`.
- The repository has paper P&L, paper positions, open paper orders, workstation portfolio snapshots, and autopilot risk events. It does not have broker account equity, broker buying power, margin, private pending orders, or live account high-water marks.
- Market executable data is available from `MarketSnapshot` top-of-book fields and optional `raw_orderbook_json`. Missing orderbook depth is treated conservatively.
- Historical edge statistics are derived only from filled paper orders joined to settlements where `settled_at < decision_timestamp`.

## Integration Point

The implemented flow is:

```text
paper signal
  -> existing eligibility checks
  -> Phase 3M Dynamic Position Sizing
  -> Phase 3N Advanced Risk Engine
  -> optional local risk reservation in live Phase 3N mode
  -> paper order builder
  -> paper fill simulator
```

Phase 3N can only preserve, reduce, or block the Phase 3M boundary quantity. It never increases size, changes side, creates live orders, or changes signal generation.

## Rollout

1. Default: `ADVANCED_RISK_ENGINE_MODE=disabled`.
2. Shadow: `ADVANCED_RISK_ENGINE_MODE=shadow` records hypothetical allow/reduce/block decisions without changing paper quantities.
3. Live local paper gate: `ADVANCED_RISK_ENGINE_MODE=live` and `ADVANCED_RISK_LIVE_MAX_CONTRACTS=1`.
4. Raise the live cap to 3, then 5 only after reviewing `reports/advanced_risk_report.md`.

Rollback is `ADVANCED_RISK_ENGINE_MODE=disabled`.

## Deferred Inputs

- Broker equity, margin, buying power, account numbers, and private broker pending orders are unavailable.
- Broker-grade depth and venue-specific market impact are unavailable unless snapshots contain orderbook data.
- Stop/target/bracket orders are not modeled by the paper ledger; Phase 3N uses binary max-loss from entry price to zero for paper risk.
- Live execution remains governed by existing execution/autopilot controls and is not enabled by Phase 3N.
