# Phase 4LK — Runtime Configuration Snapshot Contract

## Outcome

Phase 4LK defines a canonical, hash-bound snapshot of fail-closed runtime configuration. It accepts
only explicit safety fields from scheduler/UI service metadata, launcher settings, and health
evidence, and rejects rather than serializes secret-like or unknown fields.

## Safety gates

The snapshot requires disabled execution, demo execution, autopilot, and paper-order creation;
enabled dry-run and kill switches; a read-only UI; active and enabled services with bounded automatic
restart delays; bounded refresh cadence; fresh health evidence; and exactly one verified writer.

## Current runtime evidence

- Snapshot verdict: `PASS`
- Snapshot SHA-256: `50ab72c336f202c57ec0bb639045e4c745f29844736200834435859083216c60`
- Health age at observation: 172 seconds
- Writer count: 1; writer exclusivity: verified
- Refresh service: active, enabled, `Restart=always`, 15-second delay, zero service restarts
- UI service: active, enabled, `Restart=always`, 5-second delay, read-only
- Refresh cadence: 900 seconds
- All execution, demo, autopilot, and paper-creation gates remained fail-closed

The live inspection initially found the UI service marked active before its HTTP listener was
available. Restarting only that service did not produce an immediate listener because application
startup takes roughly 20 seconds; a delayed verification succeeded. This startup-latency behavior
is retained as an operational observation for Phase 4LL drift classification.

## Removal

The implementation validates supplied read-only observations and has no environment enumeration,
database, network, service-control, writer-lock, artifact-publication, or trading capability. Remove
the script, focused test, and report to roll back.

## Next phase

Phase 4LL — Runtime snapshot drift classifier and escalation policy.
