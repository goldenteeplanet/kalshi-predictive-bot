# Phase 4KJ: Recovery outcome notification

Phase 4KJ constructs an integrity-bound operator notification from the complete post-boot decision chain. It reports `RECOVERED`, `DEGRADED`, `FAILED`, or `INCOMPLETE`, assigns a stable severity, and includes every upstream decision hash for auditability.

A UI-only failure is degraded; failures in WSL, scheduler, database readability, protected invariants, or writer exclusivity are critical. Incomplete evidence is always critical. The artifact requires operator notification but intentionally grants no delivery, recovery, restart, service-control, or execution authority. Delivery remains a separate controlled capability.
