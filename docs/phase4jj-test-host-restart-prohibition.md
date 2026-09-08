# Phase 4JJ — Test-host restart prohibition

Phase 4JJ provides an absolute, integrity-bound prohibition against restarting a host that is running the supervisor's tests. Process evidence, test-environment evidence, and a test-lock artifact are independently evaluated; any positive marker produces `PROHIBITED`.

Incomplete or unverified evidence yields `DENIED`, never a permissive default. Malformed or tampered inputs and decisions fail closed. Multiple markers are retained in deterministic priority order for operator diagnosis.

`CLEAR` is only negative test-host evidence and does not authorize restart, process spawn, service control, or execution. The evaluator is read-only and contains no host inspection or operational surface; evidence collection remains a separately bounded responsibility.
