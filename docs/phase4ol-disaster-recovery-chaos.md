# Phase 4OL — Disaster-Recovery Chaos Matrix and Logical RTO

## Outcome

Phase 4OL defines an ordered nine-scenario disaster-recovery exercise: quorum loss, freeze commit,
restart replay, checkpoint loss, single-copy corruption, anchor unavailability, witness
equivocation, safe copy repair, and authorized recovery. Each hash-bound scenario has an explicit
logical-step budget and evidence reference.

Every degraded interval must remain frozen with capabilities disabled. Only the final authorized
recovery may enter the recovered state. Skipped, duplicated, or reordered transitions, per-scenario
or aggregate budget violations, missing or altered proof, unsafe intermediate state, settlement
drift, and safety exposure fail the aggregate certificate closed.

`capabilities_allowed` refers only to leaving this abstract recovery freeze; the safety envelope
continues to prohibit paper-order creation and all demo, live, and autopilot execution.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OK/4OL regression: `11 passed in 33.03s`.
- Aggregate certificate: `PASS`; scenarios: `9`.
- Logical recovery steps: `38`; aggregate budget: `38`.
- Final abstract recovery state: `RECOVERED` with execution safety prohibitions unchanged.
- Certificate SHA-256: `87d96ac35de514824cac00e17049fe45425b40631b1e667c1bb65ffd9c835ed8`.

## Safety and removal

The chaos matrix uses logical steps and offline artifacts only. It does not disrupt services, write
runtime state, create paper orders, or enable demo, live, or autopilot execution. Remove the three
Phase 4OL files to roll back.

## Next phase

Phase 4OM — Recovery-time sensitivity, tail-budget, and repeated-chaos soak proof.
