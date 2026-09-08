# Phase 4KP: Alert-loss failure injection

Phase 4KP injects loss of the primary operator alert and verifies the guarded response. A passing simulation requires secondary escalation and proves that neither component recovery nor host restart progressed without the alert prerequisite.

Missing escalation or any unsafe progression fails the injection. Acknowledgement of a deliberately suppressed primary alert is treated as tampering. The evaluator uses pre-recorded evidence only and grants no alert-delivery, recovery, restart, or execution authority.
