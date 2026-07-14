# Phase 3H: News Intelligence

Phase 3H adds local news intelligence for paper/demo analysis. It ingests RSS or manual
JSON/CSV news, classifies items with deterministic keyword rules, links news to stored Kalshi
markets, builds news features, generates news signals, and adds a bounded `news_v1` model.

It does not add live trading, real-money execution, paid APIs, or external LLM calls.

## Data Flow

```text
RSS or JSON/CSV
  -> news_items
  -> news_market_links
  -> news_features
  -> news_signals and signal_events
  -> news_v1 forecasts
  -> reports, UI, research, signal marketplace
```

## Configuration

Add RSS feeds through `NEWS_RSS_FEEDS_JSON`:

```json
[
  {
    "name": "fed",
    "url": "https://www.federalreserve.gov/feeds/press_all.xml",
    "category": "economic"
  },
  {
    "name": "bls",
    "url": "https://www.bls.gov/feed/news_release_all.rss",
    "category": "economic"
  }
]
```

If no feeds are configured, `ingest-news --source rss` exits cleanly and explains what is
missing. Manual JSON/CSV import works without RSS configuration.

## Commands

```bash
kalshi-bot ingest-news --source rss
kalshi-bot ingest-news --input-file data/news_sample.json
kalshi-bot ingest-news --input-file data/news_sample.csv
kalshi-bot link-news-markets
kalshi-bot build-news-features --window-minutes 360
kalshi-bot forecast --model news_v1
kalshi-bot news-report --output reports/news_report.md
kalshi-bot news-opportunities --model-name news_v1 --limit 20 --output reports/news_opportunities.md
kalshi-bot news-backtest --days 30 --output reports/news_backtest.md
```

## Manual JSON Format

```json
[
  {
    "source": "manual",
    "published_at": "2026-06-17T12:00:00Z",
    "title": "Fed holds rates steady",
    "summary": "The Federal Reserve held rates steady...",
    "category": "economic"
  }
]
```

CSV supports the same field names as columns.

## Classification

The classifier detects:

- crypto
- weather
- economic
- sports
- politics
- company
- geopolitical
- general

It extracts simple entities such as BTC, ETH, Fed, FOMC, CPI, jobs, hurricanes, oil/gas,
and major sports leagues. Sentiment, importance, and freshness scores are deterministic
and stored on `news_items`.

## Market Linking

`link-news-markets` compares news categories/entities/keywords against stored market title,
subtitle, ticker, series ticker, event ticker, and rules text. It creates `news_market_links`
only when confidence clears `NEWS_MIN_LINK_CONFIDENCE`.

Examples:

- BTC or Bitcoin news links to crypto/BTC markets.
- Fed/FOMC/rates/CPI/jobs news links to economic/rates markets.
- Hurricane/storm alerts link to weather markets.
- Sports news links to sports markets when enough terms match.

## Features And Signals

`build-news-features` aggregates linked news by ticker over the configured window:

- news count
- high-importance count
- average sentiment
- maximum importance
- freshness
- category/entity counts
- linked news titles

It also generates news signals:

- News Signal
- Breaking News Signal
- Economic News Signal
- Crypto News Signal
- Weather News Signal
- Sports News Signal

These flow into `signal_events`, opportunity badges, signal reports, and Research Assistant
supporting evidence.

## news_v1

`news_v1` forecasts only markets with stored `news_features`. It starts from the market
midpoint and applies a bounded adjustment using sentiment, importance, freshness, and simple
market wording direction. `NEWS_V1_MAX_ADJUSTMENT` defaults to `0.06`, and probabilities
are clamped between `0.01` and `0.99`.

If no linked news feature exists, `news_v1` skips the market.

## UI

Start the UI:

```bash
kalshi-bot ui
```

Open `/news` for:

- latest ingested news
- category counts
- linked markets
- news signals
- top news-driven opportunities

Open `/news/{id}` for a single news item, linked markets, extracted entities, and related
news_v1 opportunities.

## Limitations

- RSS availability depends on public feed uptime.
- Sports injury feeds are placeholders unless a public feed is configured manually.
- The classifier is rule-based and can miss nuance.
- The linker uses stored market text and can be ambiguous.
- Reports and forecasts are paper/demo diagnostics only.
