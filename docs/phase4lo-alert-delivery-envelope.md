# Phase 4LO — Alert Delivery-Envelope Authenticity and Routing Allowlist

## Outcome

Phase 4LO creates and validates inert, hash-bound alert envelopes only from hash-valid Phase 4LN
`EMIT` decisions. Each envelope binds its decision, alert, severity, route, fixed destination,
template, payload, creation time, and expiry.

## Validation policy

Only the local UI and local audit route classes are allowed, and both use the explicit `none`
transport. Envelopes expire within 15 minutes. Unknown routes, mutable or extra payload fields,
future or expired timestamps, duplicate hashes, broken provenance, and suppressed or refused source
decisions fail closed.

## Safety and removal

This phase validates artifacts only. Delivery, network, persistence, service control, and order
capabilities are all absent. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4LP — Alert lifecycle state-machine and terminal-state invariants.
