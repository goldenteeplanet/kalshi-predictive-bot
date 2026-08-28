# Phase 4IS — User-systemd recovery planner

Phase 4IS produces a user-scope-only symbolic recovery plan bound to a valid Phase 4IP `USER_SYSTEMD_RECOVER` capability, hashed systemd evidence, and a hashed target. Unreachable or degraded state yields user-scope verification, one manager restoration attempt, reachability verification, and user-unit verification. Reachable state yields verification only.

System scope and unknown state are denied. Wrong capability, incomplete evidence, binding mismatch, malformed data, or tampering fails closed. The plan inherits the 90-second ceiling and exactly-one-attempt rule.

The planner is read-only and dry-run-only. It never invokes `systemctl`, D-Bus, or `loginctl`, and grants no service-control, system-scope, host-restart, order, or execution authority.
