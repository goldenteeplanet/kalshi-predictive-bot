# Phase 3X — User Journeys and Screen Map

## Journey 1 — Daily orientation

1. Open `/today`.
2. Confirm environment, mode, health, as-of time, Phase 3W, and relevant Phase 3V state.
3. Review portfolio risk and system warnings.
4. Review ranked opportunities or no-trade result.
5. Open What changed.

Success: the user understands current trustworthiness before considering an opportunity.

## Journey 2 — Inspect the top opportunity

1. Select the top ranked item.
2. Read the summary thesis and invalidation conditions.
3. Compare market and model probability.
4. Review gross, cost-adjusted, and risk-adjusted EV.
5. Inspect confidence, liquidity, and support.
6. Review Phase 3S, 3M, and 3N decisions.
7. Inspect portfolio impact and lineage.

Success: the user can explain why it ranked, what could invalidate it, and why the quantity is what it is.

## Journey 3 — No trade today

1. Open Today.
2. See `TRADE_NOTHING` as the primary result.
3. Review blocking reasons and excluded-candidate counts.
4. See next evidence refresh and system status.

Success: the interface does not manufacture urgency or a weak recommendation.

## Journey 4 — Investigate a risk block

1. Open a blocked opportunity.
2. Open the decision waterfall.
3. Inspect the failing Phase 3N checks.
4. Review current and projected utilization, limit, headroom, and reason codes.
5. Navigate to contributing exposure.

Success: the user understands the block without receiving an override path.

## Journey 5 — Monitor portfolio risk

1. Open Portfolio.
2. Review P&L, daily loss, drawdown, and worst-case loss.
3. Switch exposure dimensions.
4. Drill from an aggregate to positions, orders, and reservations.
5. Open Risk for the applicable limit detail.

Success: totals reconcile and every aggregate has contributors.

## Journey 6 — Trace a trade

1. Find a trade by market, trade ID, or correlation ID.
2. Review recommendation, 3S, 3M, 3N, order, fills, position, settlement, and outcome.
3. Compare decision-time price with execution data.
4. Inspect correction history.

Success: the user can trace end to end without mixing modes or overwriting history.

## Journey 7 — Review learning

1. Open Journal.
2. Review What worked, What failed, and What changed.
3. Open evidence and sample-size details.
4. Navigate to a model, feature candidate, or ROI-policy evaluation.

Success: research findings remain distinct from production approvals.

## Journey 8 — Handle degraded data

1. A source becomes stale or disconnected.
2. The application shell changes state.
3. Affected panels show stale/partial details and last-known timestamps.
4. The user opens System Health and follows the runbook.
5. After resynchronization, the screen returns to fresh with an audit trail.

Success: the UI never displays silent stale data or a false healthy state.

## Journey 9 — Existing guarded action

1. Start from an already-authorized Phase 3A/3B/3D workflow.
2. Server validates capability and current recommendation.
3. Review side, price, quantity, maximum loss, costs, 3S/3M/3N, Phase 3V, freshness, and expiration.
4. Submit through the existing orchestrator.
5. Wait for authoritative acknowledgement.
6. Display request reference and lifecycle state.

Success: redesigned UX improves clarity without creating a new action path.

## Screen-state matrix

Every critical screen must be captured in these applicable states:

```text
loading
fresh and complete
empty valid
no trade
partial
stale
expired
disconnected
unavailable
redacted
blocked
unauthorized
error
historical replay
paper
shadow
synthetic
live read-only
```
