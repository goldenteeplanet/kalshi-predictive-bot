# Phase 4KR: Scheduler-failure injection

Phase 4KR injects failure of the authoritative scheduler and verifies detection, exactly one bounded component-recovery attempt, mandatory recovery verification, coherent writer state, and escalation when recovery fails. Uncontrolled restart progression is forbidden.

The simulation accepts either verified restoration to one authoritative writer or verified failure with zero writers and escalation. It grants no service-control, recovery, restart, or execution authority and performs no scheduler operation.
