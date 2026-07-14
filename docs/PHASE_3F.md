# Phase 3F: AI Research Assistant

Phase 3F adds a local research assistant for analyst-style opportunity explanations. It does not call external LLM APIs, require OpenAI keys, place live trades, or enable production execution.

## What It Does

- Builds structured evidence for a ticker/model from stored rankings, forecasts, market snapshots, feature snapshots, crypto/weather links, leaderboard rows, tournament results, paper positions, paper P&L, fills, backtests, and settlements.
- Generates deterministic narratives for why an opportunity exists, why it is ranked where it is, what signals support it, what could go wrong, what data is missing, and what to do next.
- Supports predefined local questions through `ask-research`.
- Stores research notes, research questions, and opportunity research snapshots.
- Generates `reports/research_report.md`.
- Adds `/research`, `/research/opportunity/{ticker}`, and `POST /research/ask` to the local UI.

## CLI

```bash
kalshi-bot research-opportunity --ticker TICKER --model-name ensemble_v2
kalshi-bot ask-research "Why is this ranked #1?" --model-name ensemble_v2
kalshi-bot research-report --model-name ensemble_v2 --limit 10 --output reports/research_report.md
```

## Recommended Local Flow

```bash
kalshi-bot collect-once --status open --limit 100 --max-pages 1
kalshi-bot forecast --model all
kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20 --output reports/opportunities_ensemble_v2.md
kalshi-bot best-payouts --model-name ensemble_v2 --limit 20 --output reports/best_payouts.md
kalshi-bot research-report --model-name ensemble_v2 --limit 10 --output reports/research_report.md
kalshi-bot ask-research "Why is this ranked #1?" --model-name ensemble_v2
kalshi-bot ui
```

## Supported Questions

- Why is this ranked #1?
- Why does the bot like this?
- Why is this risky?
- What is the main driver?
- What changed since last run?
- Should this be paper traded?
- Should this be demo dry-run only?
- What data is missing?
- Which model is driving this?
- How does this compare to market_implied_v1?
- Why did the bot skip this?
- What are the top 5 opportunities and why?

## UI

- `/research` shows the question box, top opportunities explained, top risks, missing-data warnings, and model drivers.
- `/research/opportunity/{ticker}` shows the full analyst-style writeup.
- Opportunity cards include a `Why?` link to the research writeup.
- `POST /research/ask` returns a deterministic JSON answer and stores the question/answer locally.

## Persistence

Phase 3F adds:

- `research_notes`
- `research_questions`
- `opportunity_research_snapshots`

Research snapshots are written by `research-report`. They allow simple change comparisons such as rank, score, edge, and recommendation changes since the last research run.

## Limitations

- Explanations are only as good as the stored local data.
- Missing crypto, weather, backtest, leaderboard, or snapshot data is called out explicitly.
- The assistant uses templates and evidence rules, not free-form model generation.
- All outputs are paper/demo review aids only.

## Phase 3F-1: Learning Mode + Model Confidence Engine

Phase 3F-1 extends the paper-learning workflow with a dedicated Learning Mode and confidence engine. It still does not place live trades or submit demo orders.

### What It Adds

- `LEARNING_MODE=true` by default.
- Paper-only threshold overrides for Learning Mode.
- Fast-settlement learning target generation.
- Dedicated `learning_runs`, `learning_cycles`, `learning_trade_targets`, and `model_confidence_scores` tables.
- Model confidence labels: `Leader`, `Promising`, `Needs More Data`, and `Underperforming`.
- Dynamic `model_confidence_v1` weights written into the existing `model_weights` table for `ensemble_v2`.
- `/learning` and `/models/confidence` UI pages.
- `reports/learning_report.md`, `reports/learning_targets.md`, and `reports/model_confidence.md`.

### CLI

```bash
kalshi-bot learning-status
kalshi-bot learning-once
kalshi-bot learning-run --max-cycles 32 --interval-minutes 15
kalshi-bot learning-report --output reports/learning_report.md
kalshi-bot learning-targets --limit 100 --output reports/learning_targets.md
kalshi-bot model-confidence --days 30 --output reports/model_confidence.md
```

### Safety

- Learning Mode lowers paper thresholds only.
- Demo execution is blocked when `LEARNING_MODE=true` and `LEARNING_BLOCK_DEMO_EXECUTION=true`.
- Live execution remains absent from the codebase.
- Paper orders keep duplicate forecast protection and daily Learning Mode caps.
