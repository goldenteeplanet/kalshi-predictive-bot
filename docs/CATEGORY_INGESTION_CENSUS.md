# Category ingestion census

The census is a read-only, signed report that identifies the first missing pipeline stage
for each active market across crypto, weather, sports, economic, news, general, and
composite categories.

Generate it from normalized category evidence:

```bash
PYTHONPATH=src .venv/bin/python scripts/category_ingestion_census.py \
  --input /path/to/category-evidence.json \
  --output reports/roadmap/category_ingestion_census.json
```

The input is an object keyed by canonical category. Each category can include the aggregate
source/count contract and a `markets` array. Market rows require a `ticker` and accept the
boolean stages `active`, `verified_link`, `fresh_snapshot`, `fresh_features`, `forecast`,
`ranking`, `opportunity`, `risk_evidence`, and `paper_trace`.

The report always includes all seven categories. Missing categories and aggregate-only
reports are blocked explicitly rather than treated as zero healthy markets. The artifact is
checksummed and the Refresh & Readiness page refuses tampered evidence.

This command does not fetch sources, create links or forecasts, create paper orders, lower
thresholds, call authenticated APIs, or enable live trading.
