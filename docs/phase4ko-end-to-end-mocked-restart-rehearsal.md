# Phase 4KO: End-to-end mocked restart rehearsal

Phase 4KO binds the successful dry-run trace to the Phase 4JK mock restart executor, a simulated boot-identity transition, and the post-boot workstream result. It proves that the full restart branch can be rehearsed without launching a process or restarting the host.

An injected mock-exit failure or failed post-boot chain fails the rehearsal. An unchanged simulated boot identity is treated as tampering. The result remains mock-only and grants no restart, process-spawn, recovery, or execution authority.
