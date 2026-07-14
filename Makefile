PYTHON ?= python

.PHONY: install test lint typecheck format collect-once report paper-run paper-report paper-pnl backtest compare opportunities rankings leaderboard postgres-up postgres-down postgres-logs db-health db-doctor db-migrate db-revision sqlite-backup

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy src

format:
	$(PYTHON) -m ruff format .

collect-once:
	kalshi-bot collect-once --status open --limit 100 --max-pages 1

report:
	kalshi-bot report-calibration --model-name market_implied_v1 --output reports/calibration.md

paper-run:
	kalshi-bot paper-run

paper-report:
	kalshi-bot paper-summary --output reports/paper_trading.md

paper-pnl:
	kalshi-bot paper-pnl

backtest:
	kalshi-bot backtest --model-name market_implied_v1 --strategy paper_v1 --days 30 --output reports/backtest_market_implied_v1.md

compare:
	kalshi-bot compare-strategies --days 30 --output reports/strategy_comparison.md

opportunities:
	kalshi-bot find-opportunities --model-name market_implied_v1 --limit 20 --output reports/opportunities.md

rankings:
	kalshi-bot market-rankings --limit 50 --output reports/market_rankings.md

leaderboard:
	kalshi-bot leaderboard --days 30 --output reports/model_leaderboard.md

postgres-up:
	docker compose -f docker-compose.postgres.yml up -d

postgres-down:
	docker compose -f docker-compose.postgres.yml down

postgres-logs:
	docker compose -f docker-compose.postgres.yml logs -f postgres

db-health:
	kalshi-bot db-health

db-doctor:
	kalshi-bot db-doctor

db-migrate:
	kalshi-bot db-migrate

db-revision:
	kalshi-bot db-revision --message "$(message)"

sqlite-backup:
	kalshi-bot sqlite-backup
