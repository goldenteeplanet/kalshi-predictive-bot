# Phase 3X UI/UX Audit

- Generated at: `2026-07-20T22:31:46.785681+00:00`
- Release decision: `INCOMPLETE`
- Phase 3W prerequisite: `SYSTEM_INCOMPLETE`
- Live trading authorized: `False`

## Route Inventory

| Route | Primary user job | Source authority | Action authority | Disposition | Risk |
| --- | --- | --- | --- | --- | --- |
| /today | Answer what to focus on today without hiding no-trade states. | Phase 3U/3T read models and existing dashboard services | Read-only; no domain mutations | add | Requires parity evidence before default production rollout |
| / | Legacy trader cockpit landing route | DecisionUiService.dashboard | Existing guarded action routes only | retain | Legacy density and terminology remain until route migration |
| /opportunities | Scan and investigate ranked opportunities | MarketRanking, Forecast, MarketSnapshot | Read by default; existing review route for actions | refine | Must keep fixed route ordering before /opportunities/{ticker} |
| /opportunities/{ticker} | Inspect lineage, probabilities, risk, and paper history | Market, MarketRanking, Forecast, Paper ledger | Existing execution review route | refine | Market and model probability labels must remain distinct |
| /links/coverage | Inspect parsed market legs and specialized link coverage | MarketLeg and crypto/weather/economic/sports/news link tables | Read-only diagnostics; CLI owns parsing mutations | add | Coverage evidence must not imply live-trading readiness |
| /institutional | Read-only institutional dashboard snapshot | Phase 3T dashboard snapshot service | Read-only Phase 3T boundary | retain | Cannot grow write controls |
| /portfolio | Inspect paper positions and portfolio P&L | Paper ledger and workstation repositories | Read-only investigation | refine | Gross, net, realized, and projected values must stay labeled |
| /models | Review model readiness and performance | Forecasts, confidence, model status | Read-only; no model promotion | refine | Predictive accuracy and economic profitability can be confused |
| /research | Research-only opportunity explanations and evidence | Research assistant and reports | Questions/report generation only | retain | Research candidates must not look tradable by default |
| /personal-trader | Phase 3U structured recommendation brief | Phase 3U recommendation service | No direct orders; existing guarded workflows only | refine | LLM narrative must remain subordinate to structured evidence |
| /live-readiness | Phase 3V readiness evidence and certificate state | Phase 3V readiness service | Review/audit only; no execution enablement | retain | Expired or incomplete certificates must block success claims |
| /system-certification | Phase 3W end-to-end certification evidence | Phase 3W certification service | Audit-only by default | retain | SYSTEM_INCOMPLETE must not look like production readiness |
| /system | Unified system health and release-readiness overview | DB, Phase 3V, Phase 3W, Phase 3X status cards | Read-only overview and report links | add | Shell rendering alone cannot imply healthy data |
| /settings | Presentation and database settings visibility | Settings and database status | Presentation preferences only in Phase 3X | retain | Preferences must never change domain thresholds |

## Open Findings

- Phase 3W is not SYSTEM_PASS for production rollout.
- Phase 3V readiness is not live-capital approved.
- Accessibility manual evidence is not complete.
- Visual regression baselines are not complete.
- Performance budget evidence is not complete.
- Rollback rehearsal evidence is not complete.
