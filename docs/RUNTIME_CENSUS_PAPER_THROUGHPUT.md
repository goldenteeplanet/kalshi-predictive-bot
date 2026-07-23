# Runtime census and paper-settlement throughput

Every successful GH-2 refresh now writes two signed, read-only roadmap artifacts after its
database commit:

- `reports/roadmap/category_ingestion_census.json`
- `reports/roadmap/paper_settlement_throughput.json`

The category census reads active markets, parsed categories, category link tables, fresh
snapshots and features, forecasts, rankings, opportunities, Phase 3N decisions, and complete
paper traces. Sports links count as verified only when their stored source is explicitly
`verified_schedule`; market-derived links remain blocked. Cross-category markets map to the
paper-only composite category. Runtime database evidence does not independently certify
direct external-source lineage or live eligibility.

The throughput report counts filled and settled orders by category, trades awaiting
settlement, missing order-to-fill-to-settlement-to-P&L lineage, deterministic zero-trade
reasons, overall progress toward 100 settled trades, and crypto/weather progress toward 30
settled trades each.

Both reports are checksummed. Refresh & Readiness displays them only after checksum
verification. Report generation does not create orders, update settlements or P&L, lower
thresholds, make authenticated calls, or enable live trading.
