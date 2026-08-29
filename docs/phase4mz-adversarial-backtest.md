# Phase 4MZ — Adversarial Backtest and Leakage Firewall

## Existing-interface inventory

- `src/kalshi_predictor/backtesting/engine.py` selects the latest snapshot at or before forecast
  time and can persist runs and trades.
- `src/kalshi_predictor/backtesting/strategy.py` implements the current `paper_v1` edge decision.
- `src/kalshi_predictor/backtesting/metrics.py` calculates P&L, exposure, drawdown, Brier score, and
  log loss.
- `scripts/crypto_distribution_walk_forward.py` restricts calibration to settlements available by
  forecast time.

## Outcome

Phase 4MZ adds a separate pure-record, non-persistent adversarial harness. Its event-time firewall
rejects feature or quote inputs after decision time and invalid settlement ordering. Outcomes are
used only for evaluation after decisions. Seventeen deterministic scenarios cover outcome shuffles,
future injection, stale data, execution friction, missing feeds, extreme and correlated shocks,
regime changes, parameter perturbation, and an intentionally losing control.

## Reproducible evidence

- complete Phase 4MP–4MZ plus existing backtest comparison suite: 111 passed
- deterministic scenarios: 17
- scenario-matrix SHA-256: `7fc35b36f41b264cdc20f3866b3cd6d5f777249a81d7f02b9352120ff1a7fe97`
- baseline report SHA-256: `000aa4986f739ac2b582bbbefbb1f70c3f30969f588b15b93562a8a75c7a6f77`
- baseline fixture net P&L: `-0.32` (the harness does not assume profitability)
- injected-leakage refusal SHA-256: `ddd4a86b2ea38776b843a1e093d3b5fc2512ae69b030bb48cd9db5349193c1a7`
- intentionally unprofitable control net P&L: `-3.67`

## Safety and removal

The harness does not open a database session, persist results, access a network, control services,
or expose paper, demo, live, autopilot, or order capability. Remove the three phase files to roll
back.

## Next phase

Phase 4NA — Walk-forward split discipline and purged/embargoed cross-validation proof.
