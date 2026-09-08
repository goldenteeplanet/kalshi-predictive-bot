# Phase 4LL — Runtime Snapshot Drift Classifier and Escalation Policy

## Outcome

Phase 4LL compares hash-valid Phase 4LK snapshots and classifies drift as expected transient,
benign, warning, critical, or invalid evidence. It binds both snapshot identities, field-level drift,
event duration, recurrence count, severity, action, and policy version into a canonical result.

## Hysteresis and escalation

A first UI listener delay of at most 30 seconds is monitored as startup latency. Longer or repeated
UI/service failures become restart-eligible warnings, and a third recurrence requires human
intervention. A first WSL outage is WSL-restart eligible; any repeat requires human intervention.
Trading-authority broadening, lost writer exclusivity, stale health, unsafe cadence, disabled restart
protection, and fail-open service/UI state are critical immediately.

## Current classification evidence

- The two WSL outages observed during the current engineering run classify as `CRITICAL` with
  `HUMAN_INTERVENTION_REQUIRED`; classification SHA-256:
  `d60053d7a86719c68c37af8ef723cbd446a6b8047cd4422c8ac59befd0501a4b`.
- The active and enabled services were rechecked. The UI HTTP listener was initially unavailable but
  recovered inside the 30-second observation window without a restart, matching the
  `EXPECTED_TRANSIENT` startup-latency rule.
- Thirteen focused tests pass, including malformed health-age evidence and the no-action-capability
  invariant.

## Safety and removal

Actions are recommendations only. The classifier has no WSL, service, database, network, lock,
artifact-publication, or trading capability. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4LM — Runtime observation timeline and recurrence ledger.
