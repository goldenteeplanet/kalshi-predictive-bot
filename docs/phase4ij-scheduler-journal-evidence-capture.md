# Phase 4IJ — Scheduler journal evidence capture

Phase 4IJ canonicalizes already-captured scheduler journal metadata. It retains hashed journal cursor, unit identity, and message content plus timestamp, syslog priority, byte count, completeness, and integrity data. Raw message text is excluded.

Defaults limit a capture to 256 entries and 256 KiB within an inclusive time window; exact bounds pass. Excessive or out-of-window evidence is refused, incomplete entries are `PARTIAL`, and duplicate cursors, malformed data, or tampering fails closed.

The normalizer never invokes `journalctl` or `systemctl` and cannot mutate a unit or journal. It grants no recovery, service-control, host restart, order, or execution authority.
