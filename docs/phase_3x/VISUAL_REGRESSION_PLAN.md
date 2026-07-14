# Phase 3X — Visual Regression Plan

## Baseline dimensions

Use repository-supported viewports representing:

- wide desktop institutional workspace;
- standard desktop/laptop;
- tablet landscape and portrait;
- narrow mobile monitoring.

## Required routes

Capture:

```text
Today
Opportunity scanner
Opportunity detail
Market detail
Portfolio
Risk
Trade blotter
Trade lifecycle
Model matrix
Journal
Research routes
System health
Phase 3V readiness
Phase 3W certification
Settings
```

## Required states

Capture applicable:

```text
fresh
empty valid
no trade
partial
stale
disconnected
unavailable
blocked
expired
unauthorized
error
paper
shadow
replay
synthetic
live read-only
```

## Variants

- light and dark theme;
- comfortable and compact density;
- long titles and large values;
- keyboard focus;
- reduced motion;
- 200% zoom spot checks.

## Approval rules

- Review meaningful diffs; do not accept broad percentage thresholds blindly.
- A baseline update requires a linked change reason.
- Status, warning, freshness, mode, risk, and quantity regressions are critical.
- Missing labels or hidden columns are not cosmetic.
