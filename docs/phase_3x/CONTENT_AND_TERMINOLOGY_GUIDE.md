# Phase 3X — Content and Terminology Guide

## Voice

Use calm, precise, direct language. State uncertainty and failure plainly. Avoid promotional, gamified, or emotionally loaded trading language.

## Core distinctions

| Concept | Required wording |
|---|---|
| Exchange-implied value | Market probability |
| Model estimate | Model probability |
| Difference | Edge, with side and formula available |
| Before costs | Gross EV |
| After expected costs | Cost-adjusted EV |
| After risk adjustment | Risk-adjusted EV |
| Model certainty | Confidence, with definition/version |
| Proposed by 3M | Proposed quantity |
| Final after 3N | Final quantity |
| No eligible candidate | No trade |
| Old data | Stale, with last-known timestamp |
| Missing sources | Partial, with affected sources |

## Recommendation copy

Use:

```text
Decision
Why
Key numbers
What could invalidate it
Risk and portfolio impact
Evidence freshness
Next step
```

Do not use:

```text
sure thing
guaranteed
safe trade
can't miss
lock
free money
strong play
act now
```

## Error copy

Lead with the user impact, then the state, then a correlation reference.

Example:

```text
Current opportunities cannot be verified because market quotes are stale.
Last confirmed quote: 10:42:17 AM CT.
Live actions remain unavailable. Reference: CORR-…
```

## No-trade copy

Treat no-trade as a valid decision:

```text
No opportunity currently clears the ROI, liquidity, sizing, and risk gates.
12 candidates were evaluated; 7 failed minimum economic value, 3 were too illiquid,
and 2 were blocked by portfolio exposure. Next scheduled refresh: …
```
