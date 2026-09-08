# Phase 4FM — Evidence Cold-Path Proposal

## Production evidence

- Backend: SQLite 3.45.1.
- `paper_pnl`: 140,680 snapshot rows, 204 tickers, 124,668 settled rows, 203 settled tickers.
- Indexes: `calculated_at` and `ticker`; no settlement-aware index.
- Current plan: full table scan plus temporary B-tree for `COUNT(DISTINCT)`.
- Current bounded samples: 205–263 ms without writer contention.
- Grouped/index scan: 1.37–1.52 seconds.
- Latest-per-ticker grouped scan: 1.40–1.56 seconds.
- The previously observed 2.23–5.75 second cost is consistent with the same scan under writer contention.

## Alternatives

| Alternative | Equivalent | Freshness | Writer impact | Immediate use |
|---|---:|---|---|---:|
| Current distinct count | Yes | Transaction current | Read contention only | Yes |
| Grouped ticker subquery | Yes | Transaction current | Slower read | No |
| Latest row per ticker | Not universally; latest row may be unsettled | Transaction current | Slower read | No |
| Existing activation artifact | No; covers only authorized activation | Artifact timestamp | None | No |
| Scheduler read-model artifact | Yes if built from same transaction | Watermarked | Small atomic file write | Proposed |
| Partial covering index | Yes | Transaction current | DDL/build/ongoing index cost | Proposal only |
| Materialized summary | Yes with governed refresh | Refresh watermark | New writer surface | Proposal only |

The recommended immediate production-safe choice is the Phase 4FL in-process cache. The next
candidate is a scheduler-owned, hash-protected read-model artifact after explicit review. The UI
must remain a validator/consumer and never publish the artifact.

## Non-executable index proposal

```sql
CREATE INDEX IF NOT EXISTS ix_paper_pnl_settled_ticker
ON paper_pnl (ticker)
WHERE settlement_result IS NOT NULL;
```

Rollback:

```sql
DROP INDEX IF EXISTS ix_paper_pnl_settled_ticker;
```

This is a proposal only. Applying it requires an approved maintenance window, a verified backup,
free-disk preflight, exclusive migration authority, before/after ordinary `EXPLAIN`, semantic count
comparison, integrity check, latency samples, and rollback confirmation. SQLite index construction
can block the sole writer and must not be attempted by the UI or this phase.
