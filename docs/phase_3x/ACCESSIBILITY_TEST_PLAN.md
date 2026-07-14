# Phase 3X — Accessibility Test Plan

## Standard

Target WCAG 2.2 AA. Use native HTML first and established ARIA Authoring Practices for custom widgets.

## Automated checks

Run on every critical route and representative state:

- semantic landmark and heading checks;
- accessible names;
- contrast;
- form labels and errors;
- invalid ARIA;
- focusable hidden content;
- duplicate IDs;
- table semantics;
- dialog and drawer semantics.

## Manual keyboard checks

Verify:

- skip to main content;
- primary navigation;
- command palette;
- global filters;
- opportunity scanner;
- compare tray;
- detail drawer;
- tabs;
- chart/table switch;
- risk waterfall;
- existing guarded action review;
- focus restoration and escape behavior.

## Screen-reader journeys

Test Today, opportunity detail, portfolio exposure, risk block, trade lifecycle, and system-health degraded state with at least one supported desktop screen reader/browser combination. Include a second combination where repository policy requires it.

## Zoom, reflow, and motion

- 200% zoom on desktop critical routes;
- text reflow at narrow widths;
- browser text-size increase;
- reduced-motion preference;
- high-contrast/forced-colors behavior where supported.

## Visualization alternatives

Every chart, heat map, and matrix must expose exact values, units, sample size/finality where relevant, timestamps, and state through a synchronized table or list.

## Evidence

Retain tool output, manual test notes, screenshots, defects, fixes, and final approval. Automated passing output alone is not sufficient for the critical routes.
