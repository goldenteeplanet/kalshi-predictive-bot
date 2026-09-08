# Phase 4OY — Offline WSL Restart and Service-Invariant Rehearsal

## Outcome

Phase 4OY models process crash, WSL termination, Ubuntu relaunch, systemd readiness, invariant
verification, checkpoint restoration, bot startup, UI startup, and final health in one strict
sequence. Every transition is hash-bound and must preserve the paper kill switch while paper-order,
demo, live, and autopilot execution remain disabled.

Bot startup before invariant and checkpoint verification, UI startup before the bot, missing
services, stale checkpoints, transient execution enablement, restart loops beyond three attempts,
tampering, and premature health claims fail closed. This is a rehearsal only; no WSL, service, or
machine restart occurs.

## Verification evidence

- Ruff: passed.
- Focused Phase 4OX/4OY regression: `12 passed in 40.76s`.
- Ordered transitions: `9`; rehearsal verdict: `PASS`; final modeled health: `HEALTHY`.
- Actual restart performed: `false`; executable: `false`.
- Certificate SHA-256: `c9269c6255f11d9a10c00461ed370ab1f667a425569eb049f71028970d4b0aca`.

## Safety and removal

The model is offline, in-memory, and non-persistent. It cannot create paper orders or enable demo,
live, or autopilot execution. Remove the three Phase 4OY files to roll back.

## Next phase

Phase 4OZ — Restart-rehearsal fault injection and invariant-observability proof.
