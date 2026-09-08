# Phase 4KU: Operator runbook and recovery drill

## Non-negotiable boundary

This runbook is paper-only. Do not enable live trading, create orders, modify the protected database, force a restart, or bypass warning, cancellation, cooldown, budget, loop-breaker, or post-boot gates. The automated implementation remains dry-run/read-only; operational commands require separately granted human authority.

## Alert response

1. Acknowledge the alert and record its incident and evidence hashes.
2. Verify the incident, host, WSL distribution, scheduler, database, and UI identities before acting.
3. Inspect bounded read-only evidence. Treat missing, stale, contradictory, or hash-invalid evidence as failure.
4. Confirm that paper-only and protected-invariant boundaries remain intact.

## Recovery review

5. Review the exactly-once component-recovery result. Never repeat a failed attempt automatically.
6. If restart eligibility is proposed, review the five-minute warning and cancellation token. Cancellation always wins.
7. Verify the six-hour cooldown, seven-day budget, and incident loop breaker. Any denial stops the workflow.
8. Use only the mock restart executor during drills. A real restart requires separate explicit authorization and the non-forced adapter.

## Post-boot and failure handling

9. Verify WSL, scheduler, database readability, protected invariants, writer exclusivity, and UI in order.
10. If any check fails, keep recovery automation quarantined and follow the manual disablement handoff. Do not claim recovery success.
11. Recheck the protected counts and identities before closing the incident.
12. Complete and acknowledge the content-addressed operator handoff packet.

## Protected invariants

- Paper orders: 204.
- Position sizing: maximum ID 239 and count 239.
- Advanced risk: maximum ID 239 and count 239.
- Protected filled order: ID 204, ticker `KXRAINAUSM-26AUG-1`, quantity 1.
- Protected forecast ID: 523912; protected fill count: 1.
- Phase 3M and Phase 3N maximum IDs: 231 and 231.

The accompanying drill verifier requires all twelve steps in this exact order with monotonic timestamps and simulation-only evidence. Any live operation fails the drill.
