# Phase 4II — Systemd status evidence capture

Phase 4II canonicalizes already-captured systemd unit status. It retains hashed unit identity, user/system scope, load state, active state, constrained sub-state, timestamp, completeness, and integrity data. Unit names and raw output are excluded.

The default limit is 64 units and the exact limit passes. Empty or incomplete evidence is `PARTIAL`; duplicate identities within a scope, future data, malformed data, or tampering fails closed. The same hashed identity may occur once in each distinct scope.

The normalizer never invokes `systemctl` or D-Bus and cannot start, stop, or restart a unit. It grants no recovery, service-control, host restart, order, or execution authority.
