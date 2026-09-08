# Phase 4JB — Five-minute restart warning

Phase 4JB creates a deterministic, read-only warning preview for a restart-eligible incident. The interval is exactly 300 seconds and is bound to the incident, eligibility decision, reason summary, and cancellation-command hashes. An operator-visible preview and cancellation command are mandatory.

Ineligible or misconfigured requests yield `DENIED`; partial requests yield `INCOMPLETE`; malformed or tampered inputs fail closed. The warning end time is derived from the supplied issuance time, never from an implicit clock.

`READY` certifies only the warning preview. It does not deliver a notification or authorize service control, restart, or execution. Actual warning delivery and all later restart-policy gates remain separate requirements.
