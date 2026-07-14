# Phase 3X — Design System Specification

## Visual direction

The interface should feel calm, precise, and operational. Favor clear hierarchy, neutral surfaces, strong typography, aligned numbers, and restrained semantic emphasis. Avoid casino aesthetics, glowing prices, decorative dashboards, and motion that rewards risk-taking.

## Token model

Use repository-native token tooling and define semantic roles rather than component-specific colors.

```text
color.surface.canvas
color.surface.panel
color.surface.elevated
color.surface.overlay
color.text.primary
color.text.secondary
color.text.muted
color.text.inverse
color.border.subtle
color.border.strong
color.focus
color.interactive.default
color.interactive.hover
color.interactive.active
color.status.info
color.status.success
color.status.warning
color.status.critical
color.status.stale
color.status.partial
color.status.disabled
color.risk.used
color.risk.headroom
color.risk.breach
```

Also define:

```text
type.family.ui
type.family.numeric
type.size.*
type.weight.*
line_height.*
space.*
radius.*
border_width.*
elevation.*
motion.duration.*
motion.easing.*
grid.*
breakpoint.*
density.comfortable.*
density.compact.*
```

## Typography

- Use the current approved font stack or a performance-safe system stack.
- Use tabular figures for prices, probabilities, quantities, P&L, limits, and timestamps.
- Keep hierarchy consistent: page title, section title, panel title, body, label, metadata.
- Avoid uppercase paragraphs.
- Do not use tiny text to solve density.
- Apply precision through formatters, not arbitrary truncation.

## Status grammar

Every status has:

```text
stable code
plain-language label
icon
semantic token
optional pattern or border treatment
accessible name
short explanation
```

Use one shared mapping for:

```text
healthy / degraded / failed / incomplete
fresh / aging / stale / expired / unknown
complete / partial / empty valid / unavailable / redacted
allow / reduce / block
proceed / skip
paper / shadow / replay / synthetic / live
pending / submitted / partially filled / filled / settled / corrected / finalized
```

## Core components

### Metric card

Use for one primary value plus context. Must include unit, as-of time, source/lineage access, and state. Do not use a metric card for a multi-variable decision.

### Probability pair

Always labels market and model probability independently. Shows side and as-of time. Never uses one unlabeled percentage.

### EV breakdown

Shows gross, cost-adjusted, and risk-adjusted EV with denominator and per-contract/total semantics.

### Risk utilization bar

Shows used, limit, and headroom numerically. Includes warning/breach semantics and exact values. Never relies on color only.

### Opportunity card

Contains identity, side, executable price, probabilities, edge, risk-adjusted EV, confidence, liquidity, 3S/3M/3N summary, validity, and drill-down.

### Data table

Supports keyboard navigation, server sorting, visible filters, column management, stable pagination, and accessible row actions.

### Decision waterfall

Shows the ordered 3S -> 3M -> 3N chain. A failed step remains visible and stops the path.

### Detail drawer

Preserves page context and filters. Focus is trapped only while required, restored on close, and content can be opened as a full route.

### State panels

Provide dedicated patterns for loading, empty-valid, no-trade, stale, partial, disconnected, unavailable, blocked, expired, unauthorized, and error.

## Data visualization

- Prefer bars, lines, heat maps, matrices, and tables with exact values.
- No decorative 3D charts.
- No smoothing that hides actual values.
- Mark missing and stale values.
- Provide table alternatives.
- Use consistent legends and units.
- Respect reduced motion.

## Theme and density

Support light/dark and comfortable/compact. Theme and density are presentation-only. Safety-critical labels and state remain visible in all combinations.
