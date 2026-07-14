# Phase 3X — Information Architecture

## Product map

```text
Today
├─ Ranked opportunities
├─ Portfolio and risk snapshot
├─ System/data warnings
├─ What changed
└─ Phase 3U assistant

Opportunities
├─ Scanner
├─ Compare
└─ Opportunity detail

Markets
├─ Market directory
├─ Event/series hierarchy
└─ Market detail

Portfolio
├─ Summary
├─ Positions
├─ Orders and reservations
└─ Exposure maps

Risk
├─ Portfolio risk
├─ Market risk
├─ Trade risk
├─ Decision waterfall
└─ Breaches and exceptions

Trades
├─ Blotter
├─ Lifecycle detail
├─ Settlements
└─ Outcomes

Models
├─ Model matrix
├─ Model detail
├─ Calibration
└─ Economic performance

Journal
├─ What worked
├─ What failed
├─ What changed
└─ Historical entries

Research
├─ Feature discovery
├─ Synthetic markets
└─ ROI policy evaluation

System
├─ Health
├─ Data freshness
├─ Alerts/incidents
├─ Phase 3V readiness
└─ Phase 3W certification

Settings
├─ Appearance
├─ Density
├─ Timezone
├─ Columns
└─ Saved views
```

## Navigation rules

- Keep the primary navigation short and stable.
- Place uncommon research and system functions one level below their parent.
- Preserve `snapshot_id`, `as_of`, environment, mode, account scope, and filters across investigation routes.
- Use deterministic redirects for retired Phase 3A/3C/3D/3T routes.
- Never merge research candidates into production lists without explicit labels.
- Never merge live, paper, shadow, replay, synthetic, and simulated records.

## Route ownership

| Route family | Primary source | Domain mutations allowed? | Notes |
|---|---|---:|---|
| Today | Phase 3U + Phase 3T read models | No | Recommendation and monitoring surface |
| Opportunities | Forecast/opportunity + 3S/3M/3N | No | Investigation and comparison |
| Markets | Canonical market adapters | No | Market facts and model coverage |
| Portfolio | Accounting + Phase 3N | No | Exposure and P&L investigation |
| Risk | Phase 3N | No | No override controls |
| Trades | Execution + Phase 3O | Existing guarded paths only | Read by default; actions remain outside 3T |
| Models | Registries + Phase 3O | No | Promotion remains governed elsewhere |
| Journal | Phase 3P | No | Recommendations are not mutations |
| Research | Phases 3Q/3R/3S | No | Explicit research labels |
| System | Observability + 3V/3W | No | Runbook links only |
| Settings | Preference service | Presentation writes only | No domain thresholds |

## Global context

Every route receives a normalized presentation context:

```text
environment
execution_mode
account_scope
time_mode
snapshot_id
as_of
timezone
filters
role/capabilities
phase_3w_status
phase_3v_status where relevant
```

The context is not an authorization token and cannot grant backend access.
