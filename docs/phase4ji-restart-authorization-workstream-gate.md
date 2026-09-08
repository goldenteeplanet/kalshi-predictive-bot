# Phase 4JI — Restart authorization workstream gate

Phase 4JI closes the guarded-restart policy workstream only when all nine Phase 4IZ–4JH artifacts are present, complete, integrity-verified, safety-proven, and restricted to dry-run or read-only behavior. This covers eligibility, denial reasons, warning, cancellation, persistent intent, cooldown, budget, loop breaker, and the non-forced command preview.

Missing or incomplete evidence yields `INCOMPLETE`; unverified or unsafe evidence yields `NOT_READY`; duplicate or unknown components yield `TAMPERED`. The evidence set and decision are bounded, canonical, order-independent, and integrity-bound.

`READY` certifies this workstream only. Downstream simulation and explicit operator activation remain mandatory, and the gate grants no restart, process-spawn, service-control, or execution authority.
