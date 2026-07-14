# Phase 3V Live Trading Readiness Review

Phase 3V adds an evidence-based readiness review layer for deciding whether a specific
build, config, model set, account scope, and target stage has enough evidence for real
capital consideration.

It does not enable live trading, demo execution, order creation, order replacement,
order cancelation, funding, risk overrides, or production execution.

## Commands

```bash
kalshi-bot live-readiness-status
kalshi-bot live-readiness-review --output reports/live_readiness_report.md
kalshi-bot live-readiness-guard-check
```

## Decision Semantics

- `GO`: all mandatory evidence passes, required approvals exist, and certificate
  issuance is explicitly enabled.
- `CONDITIONAL_GO`: mandatory controls pass, only approved noncritical exceptions remain,
  and certificate issuance is explicitly enabled.
- `NO_GO`: a critical or high control failed, approval is missing, or certificate issuance
  is disabled for an otherwise launchable review.
- `INCOMPLETE`: mandatory critical or high evidence has not been supplied.

The diagnostic score cannot override a failed, stale, conflicted, unverifiable, or missing
mandatory control.

## Guard Contract

`live-readiness-guard-check` verifies a certificate payload and returns whether new or
increasing risk would be allowed. With no valid certificate, the result is fail-closed:
new risk is blocked and cancel-only remains allowed.

## UI

The dashboard exposes a `Live Readiness Review` card and `/live-readiness` page. Both are
read-only review surfaces.

