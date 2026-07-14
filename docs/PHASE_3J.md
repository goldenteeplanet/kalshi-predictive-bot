# Phase 3J: Sports Intelligence

Phase 3J adds local sports intelligence for MLB, NBA, NFL, and NHL markets. It is read-only,
paper/demo only, and does not add live trading, production execution, real-money order routing,
or paid sports data APIs.

## Data Model

Sports data is stored in:

- `sports_teams`
- `sports_games`
- `sports_team_stats`
- `sports_injuries`
- `sports_odds`
- `sports_features`
- `sports_market_links`
- `sports_signals`

Raw source payloads are stored alongside normalized fields for auditability.

## Manual Import

JSON import supports top-level `league`, `teams`, `games`, `team_stats`, `injuries`, and `odds`.

```bash
kalshi-bot ingest-sports --league MLB --input-file data/mlb_sample.json
kalshi-bot ingest-sports --league NBA --input-file data/nba_sample.json
kalshi-bot ingest-sports --league NFL --input-file data/nfl_sample.json
kalshi-bot ingest-sports --league NHL --input-file data/nhl_sample.json
```

CSV import is also supported. Use `record_type` values of `team`, `game`, `team_stat`, `injury`,
or `odds` for unambiguous routing.

## Linking And Features

```bash
kalshi-bot link-sports-markets --league ALL
kalshi-bot build-sports-features --league ALL
```

The classifier detects league and market type:

- `MONEYLINE`
- `SPREAD`
- `TOTAL`
- `PLAYER_PROP`
- `TEAM_PROP`
- `SERIES`
- `CHAMPIONSHIP`
- `UNKNOWN`

Features include team strength edge, injury edge, rest edge, travel edge, odds edge, weather edge,
total edge, home/away win probability, projected total, and confidence score. Missing inputs fall
back to neutral values instead of fabricating confidence.

## Forecast Models

Sports models are:

- `mlb_v1`
- `nba_v1`
- `nfl_v1`
- `nhl_v1`
- `sports_v1`

Each model starts from the stored market midpoint and applies a bounded sports adjustment. Models
skip markets without a sports link, without a sports feature row, or without a usable midpoint.

Run:

```bash
kalshi-bot forecast --model sports_v1
kalshi-bot forecast --model mlb_v1
kalshi-bot forecast --model all
```

## Signals

Phase 3J adds Sports, league, team strength, injury, rest, odds, weather sports, and travel
signals. Signal events are inserted into `signal_events` and sports-specific rows are inserted
into `sports_signals`.

## Reports And UI

```bash
kalshi-bot sports-report --league ALL --output reports/sports_report.md
kalshi-bot sports-opportunities --model-name sports_v1 --league ALL --limit 20 --output reports/sports_opportunities.md
kalshi-bot sports-backtest --league ALL --days 30 --output reports/sports_backtest.md
kalshi-bot ui
```

UI pages:

- `/sports`
- `/sports/leagues/{league}`
- `/sports/games/{game_key}`

## Scheduler Profile

```bash
kalshi-bot scheduler-plan --profile sports-watch
```

The `sports-watch` profile is an advisory plan for a 10-minute paper/demo sports loop:
manual import when files are available, link markets, build features/signals, forecast
`sports_v1`, and write sports opportunities.

## Safety

Sports Intelligence remains local and diagnostic. It does not authenticate with Kalshi, does not
place real orders, does not add account access, and does not require paid sports APIs.

