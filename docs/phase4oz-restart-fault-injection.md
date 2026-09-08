# Phase 4OZ — Restart Fault Injection and Invariant Observability

## Outcome

Phase 4OZ injects ten fault classes at every applicable restart-rehearsal stage: transition omission,
delay, duplication, service crash, stale checkpoint, invariant drift, kill-switch loss, false
health, restart storm, and telemetry loss. The resulting 76 cases each require a deterministic,
specific observable error plus a frozen, non-executable refusal.

The campaign proves zero silent invariant violations. In particular, loss of the paper kill switch
or transient paper/demo/live/autopilot capability cannot pass any stage. Service loss and health
misreporting are also detected after startup.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OY/4OZ regression: `12 passed in 33.47s`.
- Fault classes: `10`; injected cases: `76`; silent violations: `0`.
- Campaign verdict: `PASS`; final state: `FROZEN`; executable: `false`.
- Actual restart performed: `false`.
- Campaign SHA-256: `f0cc9bc589a48eb2b0597c0ed4a608a9889ca4caa0997bc8889ab45225d4f253`.

## Safety and removal

Faults are injected into copied in-memory traces only. No WSL, service, or machine restart occurs,
and no order or execution capability is enabled. Remove the three Phase 4OZ files to roll back.

## Next phase

Phase 4PA — Restart observability coverage saturation and alert-routing proof.
