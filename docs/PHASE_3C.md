# Phase 3C Human-Readable Decision UI And Explainability

Phase 3C turns the local UI into a decision cockpit. It keeps the same demo-only and dry-run safety boundary while making opportunity review easier to understand.

## Launch

```bash
kalshi-bot ui
```

Open:

```text
http://127.0.0.1:8080
```

## Opportunity Cards

The dashboard now leads with market title and plain-English recommendation:

- `Bot would buy YES`
- `Bot would buy NO`
- `No trade recommended`

Each card shows confidence, edge in cents, score out of 100, spread, liquidity, time remaining, paper position, top reason, top risk, what the bot would do, and recommended action.

## Badge Meanings

- `Good`: no major local risk stands out.
- `Caution`: review the details before acting.
- `Risky`: a stronger risk flag is present.
- `No Trade`: the bot should skip the market.
- `Demo Only`: the UI is not a live trading surface.
- `Dry Run`: the default action records a dry-run only.
- `Stale Data`: latest stored snapshot is older than the freshness limit.
- `Low Edge`: estimated edge is too thin.
- `High Spread`: bid/ask spread may erase the edge.
- `Low Liquidity`: simulated order may be harder to fill cleanly.

## Detail Page

The detail page starts with a summary:

- recommended action
- confidence
- edge
- score
- biggest risk

Then it organizes the supporting evidence into tabs:

- Forecast
- Market Quality
- Risk Checks
- Paper History
- Backtest History
- Raw Data

Raw JSON is hidden by default under `Advanced / Raw Data`.

## Autopilot Page

The autopilot page now uses plain-English state:

- `Autopilot is OFF`
- `Autopilot is ON but DRY RUN only`
- `Autopilot is blocked`

It shows why the autopilot is blocked, the top guardrail, last cycle summary, and a checklist for demo environment, execution enabled, dry-run status, fresh data, risk limits, model allowed, and kill switch.

## Explainability Modules

Plain-English explanation helpers live in:

- `src/kalshi_predictor/explain/opportunity_explainer.py`
- `src/kalshi_predictor/explain/risk_explainer.py`
- `src/kalshi_predictor/explain/model_explainer.py`

They derive explanations from stored rankings, snapshots, forecasts, and settings. They do not call external AI services.

## CLI

```bash
kalshi-bot explain-opportunity --ticker TICKER --model-name ensemble_v2
```

The command prints a recommendation, why the market is interesting, risks, model explanation, and suggested next action.

## Safety

Phase 3C does not add live trading, real-money trading, production order routing, authenticated account access, balances, private keys, or signing. It only explains locally stored paper/demo data.
