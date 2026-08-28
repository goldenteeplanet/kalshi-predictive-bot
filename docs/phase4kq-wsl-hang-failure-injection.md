# Phase 4KQ: WSL-hang failure injection

Phase 4KQ injects a hung WSL probe and verifies bounded timeout handling. A pass requires the timeout bound to be reached, the simulated probe to be terminated, escalation to be generated, and both recovery and restart progression to remain blocked.

An early claimed timeout is treated as tampering. Missing termination, escalation, or fail-stop behavior fails the injection. The evaluator consumes recorded simulation evidence and grants no process-control, recovery, restart, or execution authority; it never invokes or terminates WSL.
