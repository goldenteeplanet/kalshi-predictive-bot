Build: Phase 3AH: Sports Placeholder Watch.

Objective:
Keep round-placeholder schedule rows blocked until source schedules name real teams, then rerun Phase 3AE after the clean team + time + market-type gate can evaluate them.

Safety:
- Do NOT add live trading.
- Do NOT enable demo execution.
- Keep Learning Mode PAPER ONLY.
- Do not submit orders to any exchange.
- Generated code must require explicit human approval for any future execution expansion.
- Reinforcement learning must remain offline/shadow until explicitly approved.
- The bot may generate reports/prompts, but must not auto-edit or auto-deploy code.

Current evidence:
- Exact settlement eligible trades: 0
- Active unsettled trades: 3
- Due or overdue trades: 3
- ETA buckets: {'overdue': 3}
- Phase 3AA-R5 closed/no-outcome rows: 0
- Phase 3AA-R5 usable outcome candidates: 0
- Fast settlement candidates: 0
- Slow settlement avoids: 54
- Sports partial links without upgrade: 259
- Sports provenance counts: {'verified_schedule': 31, 'kalshi_event_derived': 51063, 'partial_market_derived': 259}
- Phase 3AH round placeholder rows: 19
- Phase 3AH placeholder resolver safe rows: 13
- Phase 3AH placeholder resolver still placeholders: 6
- Phase 3AH placeholder watch rows: 19
- Phase 3AH placeholder watch gate: READY_FOR_PHASE3AE_SAFE_ROWS
- Phase 3AH roster rework rows: 0
- Phase 3AZ implementation queue: []
- Phase 3AZ recommended next action: No implementation gap is currently actionable; keep the refresh/watch loops running.
- Market coverage recommendations: ['sports: Review partial or legacy links']

Detected bottlenecks:
- DUE_OR_OVERDUE_SETTLEMENTS (MEDIUM): 3 paper trade(s) are due or overdue. Next: Run the Phase 3AA-R2 exact-ticker settlement harvest before realizing paper P&L.
- NO_FAST_LEARNING_CANDIDATES (MEDIUM): Learning governor found no 0-24h candidates. Next: Collect and rank more short-dated markets before starting new learning cycles.
- RL_POLICY_NOT_EVALUATED (LOW): Phase 3S has not produced an offline/shadow policy evaluation yet. Next: Run rl-evaluate after enough paper outcomes settle.
- FEATURE_DISCOVERY_NOT_RUN (LOW): Phase 3Q has not searched for new feature candidates yet. Next: Run feature-discovery-run once paper/forecast history is current.
- SELF_EVALUATION_NOT_RUN (LOW): Phase 3P has not written a self-evaluation journal yet. Next: Run self-evaluate to turn diagnostics into recurring lessons.
- PARTIAL_SPORTS_PROVENANCE (MEDIUM): 259 sports link(s) remain partial. Next: Add verified sports schedule/team ingestion to upgrade provenance.
- LEARNING_BOUNDED_DIAGNOSTICS (LOW): Phase 3AD used bounded aggregate learning diagnostics for fast post-refresh roadmap generation. Next: Run kalshi-bot learning-diagnostics separately when full rejection replay is needed.
- COVERAGE_SPORTS_LINKER_DEGRADED (LOW): sports coverage health is LINKER_DEGRADED. Next: Review partial or legacy links

Self-improvement candidates:
- Keep paper and market health fresh automatically | model: health_refresh | priority: 92 | blocked_by: none | next: kalshi-bot phase3ay-health-refresh --cycles 999 --interval-seconds 300 --all-markets
- Harvest exact ticker settlement evidence for due paper orders | model: settlement_reconciliation | priority: 88 | blocked_by: needs exact ticker source settlement evidence | next: kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2
- Route learning toward markets that settle soonest | model: learning_governor | priority: 85 | blocked_by: needs more fresh short-dated candidates | next: kalshi-bot phase3ab-learning-governor
- Search for new predictive features from paper evidence | model: feature_discovery | priority: 80 | blocked_by: none | next: kalshi-bot feature-discovery-run --run-type INCREMENTAL
- Evaluate policy actions with offline/shadow reinforcement learning | model: reinforcement_learning | priority: 80 | blocked_by: none | next: kalshi-bot rl-evaluate --enable-research
- Watch sports bracket placeholders until source schedules name teams | model: market_linking | priority: 78 | blocked_by: source still has bracket placeholders | next: kalshi-bot phase3ah-sports-placeholder-watch --output-dir reports/phase3ah_sports

Tasks:
1. Inspect the current Phase 3AA, 3AB, 3AC, and 3AD reports.
2. Use paper outcomes, feature discovery, self-evaluation, and offline/shadow RL as evidence.
3. Implement only the smallest safe layer needed for the objective above.
4. Preserve exact-ticker-only settlement realization.
5. Preserve paper-only Learning Mode and execution blocks.
6. Prefer `phase3ay-health-refresh` when the task is freshness/health automation.
7. Add or update CLI command(s) and Markdown/JSON reports.
8. Add focused tests for the new behavior and safety guarantees.
9. Run targeted pytest and `ruff check .`.

Acceptance commands:
```bash
source .venv/bin/activate
kalshi-bot phase3aa-realize --dry-run --no-sync-settlements
kalshi-bot phase3ay-health-refresh --cycles 1 --interval-seconds 0
kalshi-bot phase3ay-status
kalshi-bot phase3bb-domain-readiness --output-dir reports/phase3bb
kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2
kalshi-bot phase3bb-r2-general-source-intake --output-dir reports/phase3bb_r2_sources
kalshi-bot phase3bb-r2-general-source-evidence --output-dir reports/phase3bb_r2_sources
kalshi-bot phase3bb-r2-general-source-availability --output-dir reports/phase3bb_r2_sources
kalshi-bot phase3bb-r3-general-reclassification --output-dir reports/phase3bb_r3
kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az
kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2
kalshi-bot phase3ab-learning-governor
kalshi-bot phase3ac-sports-provenance-repair
kalshi-bot phase3af-sports-schedule-bootstrap --leagues MLB,WNBA,SOCCER --days-ahead 14 --ingest
kalshi-bot phase3ag-sports-ambiguity-coverage --output-dir reports/phase3ag
kalshi-bot phase3ag-sports-link-repair-pass --output-dir reports/phase3ag
kalshi-bot phase3ah-sports-evidence-backfill --output-dir reports/phase3ah_sports   --fetch-schedules --ingest-schedules
kalshi-bot phase3ah-round-placeholder-resolution --output-dir reports/phase3ah_sports
kalshi-bot phase3ah-sports-placeholder-watch --output-dir reports/phase3ah_sports
kalshi-bot phase3ah-roster-participant-verification --output-dir reports/phase3ah_sports
kalshi-bot phase3ae-verified-sports-connector
kalshi-bot feature-discovery-status
kalshi-bot rl-status
kalshi-bot phase-orchestrator --analyze   --output reports/phase_orchestrator.md   --next-prompt prompts/next_phase.md   --scan-limit 100
ruff check .
```

Final response should summarize:
- files changed
- commands added
- tests run
- latest bottleneck
- next recommended command
