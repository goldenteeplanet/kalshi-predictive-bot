# Phase 4LN — Alert Deduplication, Cooldown, and Acknowledgement Contract

## Outcome

Phase 4LN deterministically recommends whether a hash-valid Phase 4LM event should emit, suppress,
or refuse an alert. Alert keys bind event kind, runtime snapshot, and classification evidence.

## Policy

Exact duplicates are suppressed for a bounded 15-minute default cooldown. Severity escalation bypasses
the cooldown. Acknowledgements must match the alert key, cannot originate in the future, expire after
a bounded interval of at most 24 hours, and never suppress unrelated alerts. Invalid or replay-tampered
evidence fails closed.

## Safety and removal

The contract only returns a recommendation. It cannot deliver notifications, persist state, access a
network, control WSL or services, or place orders. Remove the script, focused test, and report to roll
back.

## Next phase

Phase 4LO — Alert delivery-envelope authenticity and routing allowlist.
