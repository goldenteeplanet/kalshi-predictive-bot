# Phase 3T Institutional Dashboard

Phase 3T adds a read-only institutional cockpit over existing local Kalshi bot state.
It is an investigation and operational-awareness layer, not a trading engine.

## Safety

- Disabled by default: `PHASE_3T_INSTITUTIONAL_DASHBOARD_ENABLED=false`.
- Modes: `disabled`, `historical_replay`, `read_only_shadow`, `read_only_live`.
- It cannot create, submit, modify, cancel, or replace orders.
- It cannot change Phase 3M sizing, Phase 3N risk decisions, model policy, features, or settlements.
- Synthetic-market state is labeled `SYNTHETIC INTERNAL NON_TRADABLE`.
- Phase 3S `PROCEED` remains a recommendation, not an order.
- Unknown values render as `n/a` or `UNKNOWN`, never as zero.

## UI

```bash
kalshi-bot ui
```

Open:

```text
/institutional
```

## Read API

```text
GET  /api/dashboard/v1/snapshots/current
POST /api/dashboard/v1/query/snapshot
POST /api/dashboard/v1/query/heatmap
POST /api/dashboard/v1/query/opportunities
POST /api/dashboard/v1/query/model-matrix
POST /api/dashboard/v1/query/exposures
POST /api/dashboard/v1/query/risk-limits
POST /api/dashboard/v1/query/trades
POST /api/dashboard/v1/query/system-health
POST /api/dashboard/v1/query/journals
POST /api/dashboard/v1/query/research
GET  /api/dashboard/v1/stream
GET  /api/dashboard/v1/export/snapshot.csv
```

Every query response includes schema version, request ID, query hash, snapshot ID,
as-of time, effective filters, source watermarks, freshness, completeness,
redactions, warnings, and panel data.

## Commands

```bash
kalshi-bot institutional-dashboard-status --enable-read-only
kalshi-bot institutional-dashboard-report --enable-read-only --output reports/institutional_dashboard.md
kalshi-bot institutional-dashboard-export --enable-read-only --output reports/institutional_dashboard_snapshot.csv
```

## Panels

- Environment and system status
- KPI ribbon
- Market heat map
- Opportunity scanner
- Model matrix
- Exposure maps
- Risk limits and decision waterfall
- Trade, fill, settlement, and outcome blotter
- System health and source freshness
- Phase 3P-3S research layers

## Current Limitations

- The local app has no SSO/RBAC layer; Phase 3T reports empty redactions.
- The stream endpoint emits a snapshot and heartbeat, not a full market delta feed.
- Reconciliation currently covers bounded paper position and paper order counts.
- Visual and accessibility coverage is via template structure and pytest smoke tests, not browser automation.
