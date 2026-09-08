# Phase 4IP — Recovery action capability model

Phase 4IP defines the closed action vocabulary and ceilings used by later recovery planners: WSL wake, keepalive restoration, user-systemd recovery, scheduler restoration, and a database readability check. Each action has an explicit timeout ceiling and exactly one permitted dry-run planning attempt.

Unknown actions, non-dry-run requests, multiple attempts, excessive timeouts, incomplete requests, malformed data, or tampering fail closed. Exact action bounds pass.

`CAPABLE` authorizes generation of a dry-run plan only. It does not authorize recovery, service control, WSL shutdown, Windows restart, order creation, or execution, and the model performs no external operation.
