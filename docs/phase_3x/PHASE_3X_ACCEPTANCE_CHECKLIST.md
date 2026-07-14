# Phase 3X — Acceptance Checklist

Use this checklist as implementation evidence. A checked item must link to code, a test, a screenshot, a report, or an approved manual review.

## Phase 3W prerequisite

- [ ] Latest Phase 3W report located.
- [ ] Report fingerprint matches the implementation target.
- [ ] Report scope covers intended environment and modes.
- [ ] `SYSTEM_PASS` verified for production rollout.
- [ ] Conditional findings, if any, are presentation-only and explicitly approved for preview/staging.
- [ ] Phase 3W UI-facing smoke subset defined.
- [ ] Phase 3W UI-facing smoke subset passes after implementation.

## Audit and architecture

- [ ] Every user-facing route is inventoried.
- [ ] Every route has a primary user job.
- [ ] Every route has source and action authority documented.
- [ ] Duplicate components are identified.
- [ ] Duplicate terminology is identified.
- [ ] Client-side domain calculations are removed or justified.
- [ ] Dead routes and placeholders are identified.
- [ ] Existing screenshots and performance baselines are captured.
- [ ] Information architecture is approved.
- [ ] Deep-link and redirect strategy is tested.

## Application shell

- [ ] Environment is visible on every route.
- [ ] Execution mode is visible on every route.
- [ ] Snapshot/as-of context is visible.
- [ ] Market-data freshness is visible.
- [ ] System health is visible.
- [ ] Phase 3W certification status is visible.
- [ ] Phase 3V status is visible where live actions are relevant.
- [ ] Primary navigation is keyboard accessible.
- [ ] Global search is keyboard accessible.
- [ ] Global search cannot perform domain mutations.
- [ ] Global filters expose all defaults.

## Today workspace

- [ ] Phase 3U structured result is authoritative.
- [ ] Ranked opportunities render correctly.
- [ ] `TRADE_NOTHING` is a first-class state.
- [ ] Recommendation expiration is visible.
- [ ] Quote age and forecast age are visible.
- [ ] Phase 3S, 3M, and 3N outcomes are visible.
- [ ] Excluded and blocked counts are visible.
- [ ] AI prose is subordinate to structured evidence.
- [ ] AI failure does not hide the structured result.
- [ ] Frozen inspection does not auto-refresh away.

## Opportunities and markets

- [ ] Market and model probability are clearly distinct.
- [ ] EV variants are labeled.
- [ ] Ranking formula and version are visible.
- [ ] Blocked opportunities cannot become actionable.
- [ ] Skipped, reduced, stale, and expired records remain discoverable.
- [ ] Table sorting is deterministic.
- [ ] Filters and pagination are server-authoritative.
- [ ] Opportunity detail preserves full lineage.
- [ ] Market facts, model estimates, and narrative are visually distinct.
- [ ] Synthetic markets are unmistakably non-tradable.

## Portfolio and risk

- [ ] Portfolio P&L source is authoritative.
- [ ] Daily loss and drawdown are authoritative.
- [ ] Exposure maps distinguish filled, open-order, reserved, and projected.
- [ ] Gross and net remain distinct.
- [ ] Phase 3N netting is reused.
- [ ] Limits and headroom are visible.
- [ ] Concentration contributors are drillable.
- [ ] Phase 3N reductions show proposed and final size.
- [ ] Phase 3N blocks show reason codes.
- [ ] Acknowledgement cannot override risk.

## Trades and actions

- [ ] Lifecycle states remain distinct.
- [ ] Paper, shadow, replay, demo, and live remain distinct.
- [ ] Settlement corrections preserve history.
- [ ] Existing action UI calls only the existing orchestrator.
- [ ] No direct exchange write client exists in frontend code.
- [ ] Server-side capability checks are enforced.
- [ ] Live/demo actions use an explicit review state.
- [ ] Review shows quantity, price, maximum loss, costs, gates, freshness, and expiration.
- [ ] Authoritative acknowledgement is required before success display.
- [ ] Idempotency and duplicate-submission behavior are tested.

## Design system and content

- [ ] Semantic design tokens are implemented.
- [ ] Components do not hard-code status colors.
- [ ] Light and dark themes preserve meaning.
- [ ] Comfortable and compact density work.
- [ ] Numeric columns use tabular figures.
- [ ] Status uses icon/label, not color alone.
- [ ] Terminology is consistent.
- [ ] Precision rules are consistent.
- [ ] No casino-style or gamified feedback exists.
- [ ] No vague “safe trade” language exists.

## Accessibility

- [ ] WCAG 2.2 AA target is documented.
- [ ] Native semantics are preferred.
- [ ] All critical routes are keyboard operable.
- [ ] Focus order and restoration are correct.
- [ ] Focus indicators are visible.
- [ ] Landmarks and headings are logical.
- [ ] Icons and statuses have accessible names.
- [ ] Charts have exact-value table alternatives.
- [ ] Hover-only information has a keyboard/text equivalent.
- [ ] Reduced-motion preference is respected.
- [ ] Contrast tests pass.
- [ ] Zoom and reflow tests pass.
- [ ] Critical screen-reader journeys pass.
- [ ] Automated and manual evidence is retained.

## Responsive and performance

- [ ] Desktop layout supports dense work.
- [ ] Tablet layout preserves critical context.
- [ ] Mobile layout preserves mode, freshness, risk, and authorization.
- [ ] Real content lengths are tested.
- [ ] Route-level code splitting is implemented.
- [ ] Expensive charts load on demand.
- [ ] Obsolete requests are canceled.
- [ ] Stream updates do not overwhelm rendering.
- [ ] LCP target is met or blocked by an approved exception.
- [ ] INP target is met or blocked by an approved exception.
- [ ] CLS target is met or blocked by an approved exception.
- [ ] Long-session memory and reconnect behavior are tested.

## Security, observability, and failure behavior

- [ ] No credentials or signing material appear in browser artifacts.
- [ ] Server-side authorization covers data, exports, saved views, and actions.
- [ ] Untrusted market/news/model text is rendered safely.
- [ ] Content security policy remains compatible.
- [ ] Route and component errors are observable.
- [ ] Correlation IDs are exposed safely for support.
- [ ] Stale, partial, disconnected, unavailable, blocked, and expired states are tested.
- [ ] A rendered shell cannot imply healthy data.
- [ ] Stream gaps trigger visible resynchronization.
- [ ] Sensitive telemetry is redacted.

## Rollout and rollback

- [ ] Route-level feature flags exist.
- [ ] Old and new routes can run side by side.
- [ ] Value-parity comparisons pass.
- [ ] Read-only staging passes.
- [ ] Paper/demo action parity passes where applicable.
- [ ] Production read-only canary passes.
- [ ] Rollback is rehearsed.
- [ ] Preference rollback/reset is safe.
- [ ] Rollback does not mutate domain state.
- [ ] Obsolete UI is removed only after stability evidence.

## Final decision

- [ ] All critical tests pass.
- [ ] All critical accessibility gates pass.
- [ ] All critical security and authorization gates pass.
- [ ] All authority boundaries pass.
- [ ] Phase 3W prerequisite remains valid.
- [ ] Residual findings are documented.
- [ ] Final decision is recorded as `GO`, `CONDITIONAL_GO`, `NO_GO`, or `INCOMPLETE`.
