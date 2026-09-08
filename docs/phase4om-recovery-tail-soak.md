# Phase 4OM — Recovery Sensitivity, Tail Budgets, and Repeated Chaos

## Outcome

Phase 4OM runs 256 deterministic seeded disaster-recovery matrices with bounded per-scenario cost
perturbations. It reports nearest-rank median, p95, p99, and worst aggregate cost plus p99 and worst
cost for every recovery scenario. Aggregate and scenario tail budgets are twice their nominal
logical-step budgets.

The independent verifier recomputes every total and percentile, validates run and scenario order,
replays the seed exactly, and requires zero unsafe degraded intervals. Short samples, reordered or
omitted runs, altered totals, percentile manipulation, tail manipulation, unsafe runs,
nondeterminism, and hash or safety drift fail closed.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OL/4OM regression: `12 passed in 29.92s`.
- Seeded runs: `256`; unsafe runs: `0`.
- Aggregate costs: median `57`, p95 `65`, p99 `67`, worst `69`; tail budget `76`.
- Soak SHA-256: `c6f21d48f1d930b47c7842d32f12dbe680cf2f74189480b83cdd2bbf3ee19171`.
- Replay verification: `PASS`; SHA-256:
  `6ed7666a31a7624563b6b156dda0d647229085038e9e4c1bd848b7d60bb7adc9`.

## Safety and removal

The soak is seeded, offline, in-memory, and non-persistent. It cannot create paper orders or enable
demo, live, or autopilot execution. Remove the three Phase 4OM files to roll back.

## Next phase

Phase 4ON — Recovery tail regression baselines and statistically bounded change detection.
