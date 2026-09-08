# Phase 4IC — Clock-skew classifier

Phase 4IC compares captured local and trusted-reference timestamps without reading or changing a live clock. Trusted references are limited to synchronized NTP, the Windows host clock, and a signed time source.

The default maximum absolute skew is 5 seconds and maximum reference uncertainty is 2 seconds; exact thresholds pass. Excess skew is `SKEWED`. Untrusted, overly uncertain, stale, incomplete, future, malformed, or tampered evidence fails closed.

Clock skew is never restart-eligible. Every non-synchronized outcome requires operator alerting and grants no recovery, service-control, WSL/Windows restart, order, or execution authority.
