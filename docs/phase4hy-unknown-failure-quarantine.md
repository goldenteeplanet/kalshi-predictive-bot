# Phase 4HY — Unknown-failure quarantine

Phase 4HY quarantines persistent failures whose class is not explicitly recognized. Recognized evidence classes are limited to `WSL_VM_UNAVAILABLE`, `SYSTEMD_USER_UNAVAILABLE`, and `SCHEDULER_UNAVAILABLE`; recognition permits only continued classification.

Unknown persistent failures require operator alerting and remain quarantined. Nonpersistent, incomplete, future, mismatched, malformed, or tampered claims fail closed. Neither a classified nor quarantined outcome authorizes recovery, service control, WSL or Windows restart, order creation, or execution.
