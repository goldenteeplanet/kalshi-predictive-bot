# Codex Goal — Phases 4FN–4JI: Overnight Paper-Only Acceleration Program

## Goal

Execute the following 100 phases sequentially in the Kalshi predictive-bot repository. Each phase must be independently tested and committed before the next phase begins.

## Repository and runtime

- Windows working repository: `C:\Users\user1\OneDrive\Documents\Dejoia Trading Bot\kalshi-predictive-bot`
- Authoritative WSL runtime: `/home/james/kalshi-runtime-src`
- Read-only UI: `http://127.0.0.1:8081`
- Sole scheduler/writer: `kalshi-fixed-rate-refresh.service`

## Non-negotiable safety contract

Every phase is paper-only, fail-closed, and read-only against production unless it explicitly operates on temporary fixtures. Never enable exchange, demo execution, live execution, autopilot, or paper-order creation. Never create production forecasts, rankings, decisions, orders, fills, or settlements. Never apply production DDL or migrations. Never start a second writer or launch a refresh loop. Never control the authoritative scheduler merely for deployment.

Before and after every phase, verify:

- `paper_orders = 204`
- `position_sizing_decisions max/count = 239/239`
- `advanced_risk_decisions max/count = 239/239`
- Order 204 remains one filled `KXRAINAUSM-26AUG-1` contract
- Forecast ID remains 523912
- Fill count remains one
- Phase 3M/3N IDs remain 231/231
- The authoritative scheduler is the sole production writer

If any invariant changes, any unexpected writer appears, or the scheduler is unhealthy beyond one expected cycle: stop immediately, do not commit, and report the evidence.

## Commit contract

For each phase:

1. Inspect the dirty worktree before editing.
2. Preserve all unrelated user changes.
3. Modify only phase-owned files.
4. Use temporary databases and temporary artifact directories for mutation tests.
5. Run focused tests, relevant regressions, formatting, and static checks.
6. Review `git diff --check` and the phase-owned diff.
7. Commit only phase-owned files using the exact commit prefix shown below.
8. Record the commit hash, tests, timings, and residual risks in the phase report.
9. Do not push unless separately authorized.
10. Continue automatically to the next phase only after the current phase is committed and all guards pass.

## Phase 4FN — Read-Model Consumer Contract

**Objective:** Implement a deterministic, production-read-only read-model consumer contract capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fn: read-model-consumer-contract`

## Phase 4FO — Read-Model Schema Compatibility Matrix

**Objective:** Implement a deterministic, production-read-only read-model schema compatibility matrix capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fo: read-model-schema-compatibility-matrix`

## Phase 4FP — Read-Model Watermark Semantics

**Objective:** Implement a deterministic, production-read-only read-model watermark semantics capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fp: read-model-watermark-semantics`

## Phase 4FQ — Read-Model Chain Validation

**Objective:** Implement a deterministic, production-read-only read-model chain validation capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fq: read-model-chain-validation`

## Phase 4FR — Read-Model Retention Policy

**Objective:** Implement a deterministic, production-read-only read-model retention policy capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fr: read-model-retention-policy`

## Phase 4FS — Read-Model Crash-Safety Simulation

**Objective:** Implement a deterministic, production-read-only read-model crash-safety simulation capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fs: read-model-crash-safety-simulation`

## Phase 4FT — Read-Model Concurrent-Reader Audit

**Objective:** Implement a deterministic, production-read-only read-model concurrent-reader audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ft: read-model-concurrent-reader-audit`

## Phase 4FU — Read-Model Staleness Escalation

**Objective:** Implement a deterministic, production-read-only read-model staleness escalation capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fu: read-model-staleness-escalation`

## Phase 4FV — Read-Model Provenance Dashboard

**Objective:** Implement a deterministic, production-read-only read-model provenance dashboard capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fv: read-model-provenance-dashboard`

## Phase 4FW — Read-Model Differential Replay

**Objective:** Implement a deterministic, production-read-only read-model differential replay capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fw: read-model-differential-replay`

## Phase 4FX — Read-Model Release Candidate

**Objective:** Implement a deterministic, production-read-only read-model release candidate capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fx: read-model-release-candidate`

## Phase 4FY — Read-Model Independent Review

**Objective:** Implement a deterministic, production-read-only read-model independent review capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fy: read-model-independent-review`

## Phase 4FZ — Read-Model Workstream Certification

**Objective:** Implement a deterministic, production-read-only read-model workstream certification capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4fz: read-model-workstream-certification`

## Phase 4GA — Settled-Count Query Contention Audit

**Objective:** Implement a deterministic, production-read-only settled-count query contention audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ga: settled-count-query-contention-audit`

## Phase 4GB — SQLite Read Transaction Budget

**Objective:** Implement a deterministic, production-read-only sqlite read transaction budget capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gb: sqlite-read-transaction-budget`

## Phase 4GC — SQLite Busy-Timeout Evidence

**Objective:** Implement a deterministic, production-read-only sqlite busy-timeout evidence capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gc: sqlite-busy-timeout-evidence`

## Phase 4GD — Evidence Query Cancellation Boundaries

**Objective:** Implement a deterministic, production-read-only evidence query cancellation boundaries capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gd: evidence-query-cancellation-boundaries`

## Phase 4GE — Evidence Query Deadline Propagation

**Objective:** Implement a deterministic, production-read-only evidence query deadline propagation capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ge: evidence-query-deadline-propagation`

## Phase 4GF — Evidence Cache Stampede Prevention

**Objective:** Implement a deterministic, production-read-only evidence cache stampede prevention capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gf: evidence-cache-stampede-prevention`

## Phase 4GG — Evidence Cache Single-Flight Proposal

**Objective:** Implement a deterministic, production-read-only evidence cache single-flight proposal capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gg: evidence-cache-single-flight-proposal`

## Phase 4GH — Evidence Cache Memory Bounds

**Objective:** Implement a deterministic, production-read-only evidence cache memory bounds capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gh: evidence-cache-memory-bounds`

## Phase 4GI — Evidence Cache Provenance Integrity

**Objective:** Implement a deterministic, production-read-only evidence cache provenance integrity capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gi: evidence-cache-provenance-integrity`

## Phase 4GJ — Evidence Cache Cold-Start Seeding Proposal

**Objective:** Implement a deterministic, production-read-only evidence cache cold-start seeding proposal capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gj: evidence-cache-cold-start-seeding-proposal`

## Phase 4GK — Evidence Query Regression Corpus

**Objective:** Implement a deterministic, production-read-only evidence query regression corpus capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gk: evidence-query-regression-corpus`

## Phase 4GL — Evidence Query Plan Drift Detector

**Objective:** Implement a deterministic, production-read-only evidence query plan drift detector capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gl: evidence-query-plan-drift-detector`

## Phase 4GM — Evidence Latency Workstream Gate

**Objective:** Implement a deterministic, production-read-only evidence latency workstream gate capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gm: evidence-latency-workstream-gate`

## Phase 4GN — Dashboard Panel Registry

**Objective:** Implement a deterministic, production-read-only dashboard panel registry capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gn: dashboard-panel-registry`

## Phase 4GO — Dashboard Progressive Disclosure

**Objective:** Implement a deterministic, production-read-only dashboard progressive disclosure capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4go: dashboard-progressive-disclosure`

## Phase 4GP — Dashboard Loading-State Contract

**Objective:** Implement a deterministic, production-read-only dashboard loading-state contract capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gp: dashboard-loading-state-contract`

## Phase 4GQ — Dashboard Partial-Data Semantics

**Objective:** Implement a deterministic, production-read-only dashboard partial-data semantics capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gq: dashboard-partial-data-semantics`

## Phase 4GR — Dashboard Staleness Visualization

**Objective:** Implement a deterministic, production-read-only dashboard staleness visualization capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gr: dashboard-staleness-visualization`

## Phase 4GS — Dashboard Lineage Explorer

**Objective:** Implement a deterministic, production-read-only dashboard lineage explorer capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gs: dashboard-lineage-explorer`

## Phase 4GT — Dashboard Settlement Timeline

**Objective:** Implement a deterministic, production-read-only dashboard settlement timeline capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gt: dashboard-settlement-timeline`

## Phase 4GU — Dashboard Evidence Lane Comparison

**Objective:** Implement a deterministic, production-read-only dashboard evidence lane comparison capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gu: dashboard-evidence-lane-comparison`

## Phase 4GV — Dashboard Accessibility Audit

**Objective:** Implement a deterministic, production-read-only dashboard accessibility audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gv: dashboard-accessibility-audit`

## Phase 4GW — Dashboard Responsive Layout Audit

**Objective:** Implement a deterministic, production-read-only dashboard responsive layout audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gw: dashboard-responsive-layout-audit`

## Phase 4GX — Dashboard Navigation Usability

**Objective:** Implement a deterministic, production-read-only dashboard navigation usability capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gx: dashboard-navigation-usability`

## Phase 4GY — Dashboard Export Safety Review

**Objective:** Implement a deterministic, production-read-only dashboard export safety review capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gy: dashboard-export-safety-review`

## Phase 4GZ — Dashboard Workstream Certification

**Objective:** Implement a deterministic, production-read-only dashboard workstream certification capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4gz: dashboard-workstream-certification`

## Phase 4HA — WSL Keepalive Reliability Audit

**Objective:** Implement a deterministic, production-read-only wsl keepalive reliability audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ha: wsl-keepalive-reliability-audit`

## Phase 4HB — WSL Boot Identity Monitor

**Objective:** Implement a deterministic, production-read-only wsl boot identity monitor capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hb: wsl-boot-identity-monitor`

## Phase 4HC — User-Systemd Session Recovery

**Objective:** Implement a deterministic, production-read-only user-systemd session recovery capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hc: user-systemd-session-recovery`

## Phase 4HD — UI Socket Readiness Gate

**Objective:** Implement a deterministic, production-read-only ui socket readiness gate capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hd: ui-socket-readiness-gate`

## Phase 4HE — UI Cold-Start Attribution

**Objective:** Implement a deterministic, production-read-only ui cold-start attribution capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4he: ui-cold-start-attribution`

## Phase 4HF — UI Graceful Shutdown Audit

**Objective:** Implement a deterministic, production-read-only ui graceful shutdown audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hf: ui-graceful-shutdown-audit`

## Phase 4HG — Scheduler Liveness Observer

**Objective:** Implement a deterministic, production-read-only scheduler liveness observer capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hg: scheduler-liveness-observer`

## Phase 4HH — Scheduler Writer-Lock Observer

**Objective:** Implement a deterministic, production-read-only scheduler writer-lock observer capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hh: scheduler-writer-lock-observer`

## Phase 4HI — Service Restart Decision Matrix

**Objective:** Implement a deterministic, production-read-only service restart decision matrix capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hi: service-restart-decision-matrix`

## Phase 4HJ — Runtime Recovery Dry Run

**Objective:** Implement a deterministic, production-read-only runtime recovery dry run capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hj: runtime-recovery-dry-run`

## Phase 4HK — Runtime Recovery Evidence Bundle

**Objective:** Implement a deterministic, production-read-only runtime recovery evidence bundle capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hk: runtime-recovery-evidence-bundle`

## Phase 4HL — Runtime Resilience Independent Review

**Objective:** Implement a deterministic, production-read-only runtime resilience independent review capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hl: runtime-resilience-independent-review`

## Phase 4HM — Runtime Resilience Gate

**Objective:** Implement a deterministic, production-read-only runtime resilience gate capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hm: runtime-resilience-gate`

## Phase 4HN — GitHub Repository Baseline Audit

**Objective:** Implement a deterministic, production-read-only github repository baseline audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hn: github-repository-baseline-audit`

## Phase 4HO — GitHub Required Checks Matrix

**Objective:** Implement a deterministic, production-read-only github required checks matrix capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ho: github-required-checks-matrix`

## Phase 4HP — GitHub CODEOWNERS Proposal

**Objective:** Implement a deterministic, production-read-only github codeowners proposal capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hp: github-codeowners-proposal`

## Phase 4HQ — GitHub Branch Protection Proposal

**Objective:** Implement a deterministic, production-read-only github branch protection proposal capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hq: github-branch-protection-proposal`

## Phase 4HR — GitHub Dependency Review Workflow

**Objective:** Implement a deterministic, production-read-only github dependency review workflow capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hr: github-dependency-review-workflow`

## Phase 4HS — GitHub Secret Scanning Workflow

**Objective:** Implement a deterministic, production-read-only github secret scanning workflow capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hs: github-secret-scanning-workflow`

## Phase 4HT — GitHub SBOM Workflow

**Objective:** Implement a deterministic, production-read-only github sbom workflow capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ht: github-sbom-workflow`

## Phase 4HU — GitHub Artifact Attestation Workflow

**Objective:** Implement a deterministic, production-read-only github artifact attestation workflow capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hu: github-artifact-attestation-workflow`

## Phase 4HV — GitHub Reproducible Test Workflow

**Objective:** Implement a deterministic, production-read-only github reproducible test workflow capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hv: github-reproducible-test-workflow`

## Phase 4HW — GitHub Changed-File Test Routing

**Objective:** Implement a deterministic, production-read-only github changed-file test routing capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hw: github-changed-file-test-routing`

## Phase 4HX — GitHub Performance Benchmark Workflow

**Objective:** Implement a deterministic, production-read-only github performance benchmark workflow capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hx: github-performance-benchmark-workflow`

## Phase 4HY — GitHub Least-Privilege App Review

**Objective:** Implement a deterministic, production-read-only github least-privilege app review capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hy: github-least-privilege-app-review`

## Phase 4HZ — GitHub Integration Gate

**Objective:** Implement a deterministic, production-read-only github integration gate capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4hz: github-integration-gate`

## Phase 4IA — Execution Boundary Static Scanner

**Objective:** Implement a deterministic, production-read-only execution boundary static scanner capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ia: execution-boundary-static-scanner`

## Phase 4IB — Production Writer Exclusivity Test

**Objective:** Implement a deterministic, production-read-only production writer exclusivity test capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ib: production-writer-exclusivity-test`

## Phase 4IC — Paper-Order Creation Guard Test

**Objective:** Implement a deterministic, production-read-only paper-order creation guard test capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ic: paper-order-creation-guard-test`

## Phase 4ID — Exchange Credential Isolation Audit

**Objective:** Implement a deterministic, production-read-only exchange credential isolation audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4id: exchange-credential-isolation-audit`

## Phase 4IE — Live-Mode Configuration Refusal

**Objective:** Implement a deterministic, production-read-only live-mode configuration refusal capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ie: live-mode-configuration-refusal`

## Phase 4IF — Demo-Mode Execution Refusal

**Objective:** Implement a deterministic, production-read-only demo-mode execution refusal capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4if: demo-mode-execution-refusal`

## Phase 4IG — Autopilot Refusal Verification

**Objective:** Implement a deterministic, production-read-only autopilot refusal verification capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ig: autopilot-refusal-verification`

## Phase 4IH — Forecast Mutation Surface Audit

**Objective:** Implement a deterministic, production-read-only forecast mutation surface audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ih: forecast-mutation-surface-audit`

## Phase 4II — Decision Mutation Surface Audit

**Objective:** Implement a deterministic, production-read-only decision mutation surface audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ii: decision-mutation-surface-audit`

## Phase 4IJ — Order Mutation Surface Audit

**Objective:** Implement a deterministic, production-read-only order mutation surface audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ij: order-mutation-surface-audit`

## Phase 4IK — Settlement Mutation Surface Audit

**Objective:** Implement a deterministic, production-read-only settlement mutation surface audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ik: settlement-mutation-surface-audit`

## Phase 4IL — Safety Regression Bundle

**Objective:** Implement a deterministic, production-read-only safety regression bundle capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4il: safety-regression-bundle`

## Phase 4IM — Safety Workstream Certification

**Objective:** Implement a deterministic, production-read-only safety workstream certification capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4im: safety-workstream-certification`

## Phase 4IN — Evidence Sample Sufficiency Dashboard

**Objective:** Implement a deterministic, production-read-only evidence sample sufficiency dashboard capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4in: evidence-sample-sufficiency-dashboard`

## Phase 4IO — Independent-Event Diversity Audit

**Objective:** Implement a deterministic, production-read-only independent-event diversity audit capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4io: independent-event-diversity-audit`

## Phase 4IP — Calibration Stability Windows

**Objective:** Implement a deterministic, production-read-only calibration stability windows capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ip: calibration-stability-windows`

## Phase 4IQ — Shadow-to-Paper Differential Analysis

**Objective:** Implement a deterministic, production-read-only shadow-to-paper differential analysis capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4iq: shadow-to-paper-differential-analysis`

## Phase 4IR — Paper Outcome Attribution

**Objective:** Implement a deterministic, production-read-only paper outcome attribution capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ir: paper-outcome-attribution`

## Phase 4IS — Model Confidence Drift Monitor

**Objective:** Implement a deterministic, production-read-only model confidence drift monitor capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4is: model-confidence-drift-monitor`

## Phase 4IT — Feature Freshness Attribution

**Objective:** Implement a deterministic, production-read-only feature freshness attribution capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4it: feature-freshness-attribution`

## Phase 4IU — Market-Link Coverage Attribution

**Objective:** Implement a deterministic, production-read-only market-link coverage attribution capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4iu: market-link-coverage-attribution`

## Phase 4IV — Settlement Lag Distribution

**Objective:** Implement a deterministic, production-read-only settlement lag distribution capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4iv: settlement-lag-distribution`

## Phase 4IW — Forecast Deadline Compliance

**Objective:** Implement a deterministic, production-read-only forecast deadline compliance capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4iw: forecast-deadline-compliance`

## Phase 4IX — Risk Decision Latency Attribution

**Objective:** Implement a deterministic, production-read-only risk decision latency attribution capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ix: risk-decision-latency-attribution`

## Phase 4IY — Research Evidence Release Candidate

**Objective:** Implement a deterministic, production-read-only research evidence release candidate capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4iy: research-evidence-release-candidate`

## Phase 4IZ — Research Workstream Certification

**Objective:** Implement a deterministic, production-read-only research workstream certification capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4iz: research-workstream-certification`

## Phase 4JA — Operator Alert Taxonomy

**Objective:** Implement a deterministic, production-read-only operator alert taxonomy capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ja: operator-alert-taxonomy`

## Phase 4JB — Operator Alert Deduplication

**Objective:** Implement a deterministic, production-read-only operator alert deduplication capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4jb: operator-alert-deduplication`

## Phase 4JC — Operator Escalation Boundaries

**Objective:** Implement a deterministic, production-read-only operator escalation boundaries capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4jc: operator-escalation-boundaries`

## Phase 4JD — Operator Runbook Navigation

**Objective:** Implement a deterministic, production-read-only operator runbook navigation capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4jd: operator-runbook-navigation`

## Phase 4JE — Operator Incident Timeline

**Objective:** Implement a deterministic, production-read-only operator incident timeline capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4je: operator-incident-timeline`

## Phase 4JF — Operator Audit Export

**Objective:** Implement a deterministic, production-read-only operator audit export capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4jf: operator-audit-export`

## Phase 4JG — Operator Evidence Handoff

**Objective:** Implement a deterministic, production-read-only operator evidence handoff capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4jg: operator-evidence-handoff`

## Phase 4JH — Final Overnight Validation Bundle

**Objective:** Implement a deterministic, production-read-only final overnight validation bundle capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4jh: final-overnight-validation-bundle`

## Phase 4JI — Paper-Only Acceleration Final Gate

**Objective:** Implement a deterministic, production-read-only paper-only acceleration final gate capability that reduces time-to-trade uncertainty without broadening execution authority.

**Required deliverables:**

- Add a narrowly scoped implementation or non-executable proposal with explicit inputs, outputs, schema/version identity, provenance, freshness rules, bounded resource use, and fail-closed behavior.
- Add deterministic tests for the valid path, empty input, exact boundaries, staleness, malformed input, tampering, partial failure, and proof that production mutation methods are never invoked.
- Add or update a Markdown phase report containing measured evidence, rejected alternatives, safety analysis, rollback or removal procedure, and the next dependency.
- Integrate with the UI only when useful, keeping expensive work opt-in and exposing unavailable/stale states honestly.
- Verify all protected counts, order-204 lineage, service health, and writer exclusivity before completion.

**Acceptance criteria:** Focused tests and relevant regressions pass; no production DB/artifact mutation occurs; no safety setting changes; query/runtime work is bounded; the phase report is reproducible from committed files.

**Commit:** `phase4ji: paper-only-acceleration-final-gate`

## Final completion report

After Phase 4JI, provide a table of all 100 phases with commit hash, tests, artifacts, measured impact, and residual risk. Include cumulative test results, final runtime/service state, final guarded invariants, writer-exclusivity evidence, uncommitted-file inventory, and the recommended next Codex Goal. Do not claim trade readiness or enable execution.

