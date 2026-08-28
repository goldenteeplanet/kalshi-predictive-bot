# Phase 4IB — Disk-exhaustion classifier

Phase 4IB classifies captured filesystem evidence without reading a live filesystem. Healthy headroom requires at least 1 GiB free, at least 5% free bytes, at least 5% free inodes, and a writable mount. Exact thresholds pass.

Low byte or inode headroom is `EXHAUSTED`; a read-only mount is `READ_ONLY`; zero denominators or stale evidence are `UNKNOWN`; incomplete, future, contradictory, malformed, or tampered evidence fails closed.

Disk exhaustion and read-only filesystems are never restart-eligible. Non-healthy outcomes require operator alerting and grant no recovery, service-control, WSL/Windows restart, order, or execution authority.
