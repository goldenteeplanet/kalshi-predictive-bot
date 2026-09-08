# Phase 4JH — Non-forced restart command adapter

Phase 4JH produces a deterministic, hash-bound preview of the sole allowlisted Windows restart vector: `shutdown.exe /r /t 0`. The adapter explicitly rejects `/f`, `-Force`, `/force`, alternate executors, argument drift, non-dry-run requests, partial evidence, malformed data, and tampering.

`PREVIEW_READY` proves only that the command vector is exact and non-forced. It grants no restart, process-spawn, service-control, or execution authority. The implementation has no subprocess or shell surface and cannot run its preview.

Actual use remains disabled pending the restart-authorization workstream gate, test-host prohibition, mock and disposable-sandbox rehearsals, deployment controls, and operator activation. Rollback is removal of this pure adapter, its tests, and this report.
