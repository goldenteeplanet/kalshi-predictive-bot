# Runtime census and paper-settlement throughput

Every successful GH-2 refresh writes two signed, read-only roadmap artifacts after its
database commit and after releasing the shared SQLite writer lock:

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
settled trades each. It reads a bounded recent paper-order window and scopes related fill,
settlement, P&L, market, and market-leg queries to that window.

The scheduled GH-2 owner scopes the category census to the current candidate manifest and
caps it at 40 tickers. Diagnostics have a separate 45-second timeout and do not turn a
successfully committed decision refresh into a failed writer cycle. Their `COMPLETE`,
`TIMED_OUT`, `FAILED`, or `SKIPPED_DEADLINE` state is recorded separately in
`gh2_scheduler_status.json`. The diagnostics command uses a low-memory CLI fast path instead
of importing the full operator command graph.

The commit-critical refresh has a 300-second budget inside a 345-second internal service
deadline. Per-stage durations are written to `gh2_stage.json` and copied into scheduler
telemetry. Termination and unexpected-exit traps publish a terminal scheduler status so a
systemd timeout cannot leave the owner at stale `RUNNING`.

Both reports are checksummed. Refresh & Readiness displays them only after checksum
verification. Report generation does not create orders, update settlements or P&L, lower
thresholds, make authenticated calls, or enable live trading.

After deploying a runtime-budget change, keep GH-4 observational until two consecutive
scheduled GH-2 cycles finish in less than six minutes with top-level status `COMPLETE`, fresh
artifacts, and `writer_count=0`. Only then resume watching for `paper_ready > 0` and run the
read-only GH-4 activation preflight.
