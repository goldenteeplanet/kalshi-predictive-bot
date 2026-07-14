# Phase 3R Synthetic Markets

Phase 3R creates internal synthetic event-contract probability cards for research. A synthetic market is not a Kalshi listing, order book, order, fill, position, paper trade, demo trade, or live trade.

User-facing cards and reports must show:

```text
INTERNAL SYNTHETIC FORECAST — NOT A LISTED OR TRADABLE KALSHI MARKET
```

## Safety

- Disabled by default: `PHASE_3R_SYNTHETIC_MARKETS_ENABLED=false`.
- No exchange write endpoints are used.
- No paper, demo, or live orders are created.
- No opportunities or positions are created.
- Phase 3O writes are limited to `market_memory` and `forecast_memory`.
- `trade_memory` remains untouched unless a real listed-market trade occurs through the existing pipeline.

## Candidate Input

Use JSON containing one object, a list of objects, or `{ "candidates": [...] }`.

Each candidate should include:

- `category`: `WEATHER`, `ECONOMIC`, `CRYPTO`, `SPORTS`, or `GENERAL`
- `canonical_title`
- `plain_language_summary`
- `observation_window.start_at`
- `observation_window.end_at`
- `settlement_rule.primary_source_id`
- `settlement_rule.source_field`
- `settlement_rule.rule_text`
- `contracts[].canonical_question`
- `contracts[].condition`

## Commands

```bash
kalshi-bot synthetic-markets-status
kalshi-bot synthetic-markets-run --enable-research --mode shadow --input-file data/synthetic_markets_candidates.json --output reports/synthetic_markets_report.md --json-output reports/synthetic_markets_report.json
kalshi-bot synthetic-markets-report --output reports/synthetic_markets_report.md
kalshi-bot scheduler-plan --profile synthetic-markets-nightly
```

If the local Kalshi market inventory is missing or truncated, the listing check returns `LISTING_STATUS_UNKNOWN` and does not claim the candidate is unlisted.
