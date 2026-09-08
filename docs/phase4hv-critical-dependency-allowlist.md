# Phase 4HV — Critical dependency allowlist

Phase 4HV establishes the closed dependency taxonomy used by later failure-classification phases. Only `WSL_VM`, `SYSTEMD_USER_MANAGER`, and `KALSHI_SCHEDULER` are allowlisted local runtime dependencies. Network, DNS, exchange API, database, disk, clock, and unknown dependency failures are explicitly outside this boundary.

Observations are integrity-bound, complete, time-ordered records. Malformed, incomplete, future-dated, tampered, or non-allowlisted observations fail closed. An `ALLOWLISTED` result is classification evidence only: it grants no recovery, service-control, host-restart, order, or execution authority.
