# Phase 4JK — Mock restart executor

Phase 4JK records deterministic simulated invocations of the exact non-forced restart preview and returns an injected exit outcome. It requires both simulation mode and dry-run, accepts only `shutdown.exe /r /t 0`, and explicitly refuses force flags, alternate commands, partial requests, malformed fields, and tampering.

Exit code zero yields `SIMULATED`; a bounded nonzero injected code yields `SIMULATED_FAILURE`. Both are evidence about the mock path only. The executor never invokes a runner, shell, subprocess, Windows API, service, database, or notification provider.

Every result remains read-only and mock-only and grants no restart, process-spawn, service-control, or execution authority. Rollback is removal of this isolated simulator, its tests, and this report.
