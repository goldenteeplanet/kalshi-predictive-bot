# Codex Goal — Phases 4HB–4KW: Resilient Paper-Only Operations

## Goal

Execute the following 100 phases sequentially in the Kalshi predictive-bot repository. Each phase
must be independently tested and committed before the next begins. Build a fail-closed supervisory
layer that detects WSL, scheduler, database-readability, disk, clock, network, and UI failures;
alerts the operator; attempts bounded recovery; and, only for an explicitly classified unrecoverable
host condition, performs one guarded Windows restart under the policy below.

## Repository and runtime

- Windows repository: `C:\Users\user1\OneDrive\Documents\Dejoia Trading Bot\kalshi-predictive-bot`
- Authoritative WSL runtime: `/home/james/kalshi-runtime-src`
- Read-only UI: `http://127.0.0.1:8081`
- Sole scheduler/writer: `kalshi-fixed-rate-refresh.service`

## Non-negotiable trading safety contract

All work remains paper-only and fail-closed. Never enable exchange, demo execution, live execution,
autopilot, or paper-order creation. Never create production forecasts, rankings, decisions, orders,
fills, or settlements during tests or recovery. Never apply production DDL. Never start a second
writer. Recovery may restore the already-authorized sole scheduler only after writer exclusivity and
all protected invariants pass.

Before and after every phase and every recovery action, verify:

- `paper_orders = 204`
- `position_sizing_decisions max/count = 239/239`
- `advanced_risk_decisions max/count = 239/239`
- Order 204 remains one filled `KXRAINAUSM-26AUG-1` contract
- Forecast ID remains 523912; fill count remains one; Phase 3M/3N IDs remain 231/231
- `kalshi-fixed-rate-refresh.service` is the sole production writer

If an invariant changes or an unexpected writer appears, quarantine automation, alert immediately,
do not restart any service or machine, do not commit, and preserve diagnostic evidence.

## Alert and guarded-restart policy

1. Install nothing and enable no startup, notification, service-control, or reboot mechanism until its
   configuration, dry-run, tests, rollback, and operator-visible preview are complete.
2. Alerts must be local and durable by default (Windows toast when available plus append-only local
   incident record). External messaging requires separate credentials and approval. Alert on detection,
   recovery attempt, recovery success/failure, reboot scheduled, reboot cancelled, and post-boot result.
3. Only allowlisted critical failures qualify: WSL unavailable, keepalive absent, user systemd
   unreachable, authoritative scheduler inactive, production database unreadable, critical disk
   exhaustion, or material clock skew. UI or network failure alone never authorizes a host restart.
4. Use escalation: observe twice across 120 seconds; alert; collect bounded diagnostics; attempt one
   component-level recovery; wait up to 180 seconds; recheck health, invariants, and writer exclusivity.
5. Windows restart is permitted only when component recovery failed, evidence is complete, trading is
   fail-closed, no invariant changed, no second writer exists, and the failure classifier explicitly
   returns `HOST_RESTART_REQUIRED`. Tests, malformed evidence, unknown failures, and partial evidence
   must return `RESTART_DENIED`.
6. Before reboot, issue a 5-minute warning with a cancellation command. Persist the incident, reason,
   hashes, attempt counter, and post-boot verification marker outside WSL. Never use a forced reboot.
7. Loop breaker: at most one host restart in six hours and two in seven days. A failed post-boot check
   disables automatic recovery and requires the operator. State corruption or missing cooldown state
   fails closed. The supervisor must never restart the machine that is running its own tests.
8. Every control action must support dry-run. Unit/integration tests use mocks or disposable fixtures;
   they may not invoke `shutdown.exe`, `Restart-Computer`, `wsl --shutdown`, `systemctl start/restart`,
   database writers, or notification providers.

## Per-phase delivery and commit contract

For every phase: inspect the dirty tree; preserve unrelated changes; modify and commit only phase-owned
files; use temporary fixtures for mutations; define versioned inputs/outputs, provenance, freshness,
bounds, deterministic ordering, tamper evidence, and fail-closed behavior; test valid, empty, boundary,
stale, malformed, tampered, partial-failure, and forbidden-mutation paths; run focused tests, relevant
regressions, Ruff, and `git diff --check`; record test counts/timing, rollback, measured evidence, and
residual risk; do not push. Continue only after guards pass. Use commit message
`phase<code>: <kebab-case-title>`.

## Phase matrix (exactly 100 phases)

Each row inherits the contracts above and requires an implementation or non-executable proposal,
deterministic tests, and `docs/phase<code>-<slug>.md`.

| # | Phase | Capability |
|---:|:---:|---|
| 1 | 4HB | WSL boot-identity change monitor |
| 2 | 4HC | WSL liveness evidence collector |
| 3 | 4HD | Keepalive gap classifier |
| 4 | 4HE | User-systemd reachability probe |
| 5 | 4HF | Authoritative scheduler health probe |
| 6 | 4HG | Writer-exclusivity recovery gate |
| 7 | 4HH | Protected-invariant recovery gate |
| 8 | 4HI | Recovery evidence canonicalization |
| 9 | 4HJ | Recovery decision tamper detection |
| 10 | 4HK | WSL reliability workstream gate |
| 11 | 4HL | Local incident journal schema |
| 12 | 4HM | Append-only incident writer |
| 13 | 4HN | Windows toast capability probe |
| 14 | 4HO | Alert severity and deduplication |
| 15 | 4HP | Alert retry and backoff policy |
| 16 | 4HQ | Alert rate-limit and storm control |
| 17 | 4HR | Operator acknowledgement contract |
| 18 | 4HS | Recovery-cancellation command |
| 19 | 4HT | Alert delivery audit export |
| 20 | 4HU | Alerting workstream gate |
| 21 | 4HV | Critical dependency allowlist |
| 22 | 4HW | Failure observation quorum |
| 23 | 4HX | Failure persistence window |
| 24 | 4HY | Unknown-failure quarantine |
| 25 | 4HZ | Partial-evidence refusal |
| 26 | 4IA | Database-readability classifier |
| 27 | 4IB | Disk-exhaustion classifier |
| 28 | 4IC | Clock-skew classifier |
| 29 | 4ID | Network-failure non-restart rule |
| 30 | 4IE | Failure-classification workstream gate |
| 31 | 4IF | Bounded diagnostics collector |
| 32 | 4IG | Process-tree evidence capture |
| 33 | 4IH | WSL status evidence capture |
| 34 | 4II | Systemd status evidence capture |
| 35 | 4IJ | Scheduler journal evidence capture |
| 36 | 4IK | Disk and memory evidence capture |
| 37 | 4IL | Network and DNS evidence capture |
| 38 | 4IM | Clock-source evidence capture |
| 39 | 4IN | Diagnostic redaction audit |
| 40 | 4IO | Diagnostics workstream gate |
| 41 | 4IP | Recovery action capability model |
| 42 | 4IQ | WSL wake dry-run planner |
| 43 | 4IR | Keepalive restoration dry-run planner |
| 44 | 4IS | User-systemd recovery planner |
| 45 | 4IT | Scheduler restoration planner |
| 46 | 4IU | Database-readability recovery planner |
| 47 | 4IV | Recovery timeout propagation |
| 48 | 4IW | Recovery cancellation propagation |
| 49 | 4IX | Component recovery differential replay |
| 50 | 4IY | Component recovery workstream gate |
| 51 | 4IZ | Host-restart eligibility model |
| 52 | 4JA | Restart denial reason taxonomy |
| 53 | 4JB | Five-minute restart warning |
| 54 | 4JC | Restart cancellation token |
| 55 | 4JD | Persistent restart intent record |
| 56 | 4JE | Six-hour restart cooldown |
| 57 | 4JF | Seven-day restart budget |
| 58 | 4JG | Restart-loop circuit breaker |
| 59 | 4JH | Non-forced restart command adapter |
| 60 | 4JI | Restart authorization workstream gate |
| 61 | 4JJ | Test-host restart prohibition |
| 62 | 4JK | Mock restart executor |
| 63 | 4JL | Disposable recovery sandbox |
| 64 | 4JM | Crash-boundary simulation |
| 65 | 4JN | Power-loss state simulation |
| 66 | 4JO | Corrupt cooldown-state refusal |
| 67 | 4JP | Concurrent supervisor exclusion lock |
| 68 | 4JQ | Supervisor crash recovery |
| 69 | 4JR | Replay idempotency verification |
| 70 | 4JS | Recovery safety simulation gate |
| 71 | 4JT | Windows startup-task proposal |
| 72 | 4JU | Least-privilege task identity audit |
| 73 | 4JV | Startup ordering and delay model |
| 74 | 4JW | Supervisor singleton enforcement |
| 75 | 4JX | Supervisor heartbeat artifact |
| 76 | 4JY | Supervisor self-health monitor |
| 77 | 4JZ | Configuration signature validation |
| 78 | 4KA | Safe configuration reload |
| 79 | 4KB | Installation rollback package |
| 80 | 4KC | Supervisor deployment gate |
| 81 | 4KD | Post-boot WSL verification |
| 82 | 4KE | Post-boot scheduler verification |
| 83 | 4KF | Post-boot database-readability verification |
| 84 | 4KG | Post-boot invariant verification |
| 85 | 4KH | Post-boot writer-exclusivity verification |
| 86 | 4KI | Post-boot UI availability verification |
| 87 | 4KJ | Recovery outcome notification |
| 88 | 4KK | Failed post-boot automation disablement |
| 89 | 4KL | Operator handoff packet |
| 90 | 4KM | Post-boot workstream gate |
| 91 | 4KN | End-to-end dry-run rehearsal |
| 92 | 4KO | End-to-end mocked restart rehearsal |
| 93 | 4KP | Alert-loss failure injection |
| 94 | 4KQ | WSL-hang failure injection |
| 95 | 4KR | Scheduler-failure injection |
| 96 | 4KS | Cooldown and loop-breaker stress test |
| 97 | 4KT | Independent safety verifier |
| 98 | 4KU | Operator runbook and recovery drill |
| 99 | 4KV | Resilient-operations release candidate |
| 100 | 4KW | Final guarded-recovery certification |

## Installation and activation gates

Phases 4HB–4KS are implementation, evidence, simulation, and dry-run only. Phase 4KT must independently
verify the full chain. Phase 4KU requires an operator-readable rehearsal report. Phase 4KV may package
but not enable the supervisor. Phase 4KW may install and enable local alerting and the supervisor only
if every prior gate passes. The real restart adapter remains disabled until its exact command preview,
cooldowns, cancellation path, and post-boot behavior are verified. Activation must record a reversible
Windows task/service configuration and its removal command. No phase authorizes a restart merely to
prove that restart works.

## Final completion report

After 4KW, provide all 100 phases with commit hash, tests, artifacts, measured impact, and residual risk;
cumulative validation; final WSL/service state; guarded invariants; writer exclusivity; supervisor and
alert status; restart policy state and remaining budget; uncommitted inventory; exact rollback steps;
and the recommended next Codex Goal. Do not claim trade readiness or broaden execution authority.
